"""Work found before the payment needs it, so an agent with no node can send.

The third reason an agent cannot pay in XNO is work. A send needs 64x the
proof-of-work of a receive - `0xfffffff800000000` against
`0xfffffe0000000000` - and no free public node answers `work_generate`. So a
wallet can be funded by a stranger and still be unable to spend: `work.solve`
refuses the send threshold inline, because spending minutes of CPU between
"pay this" and the block going out is worse than saying the node owes work.

Nothing about that threshold needs the payment to be waiting, though. Work is
computed over the account's FRONTIER - the hash of its latest block - and
nothing else: not the amount, not the destination, not the signature. The
frontier is fixed the moment the account's last block confirms, which is
typically long before the next payment is asked for. So the work for the next
send can be found then, in the background, on every core, and read from a file
in microseconds when the payment arrives.

That is all this module is: a directory of verified work, keyed by root.

    $ nano-wallet work pending                  # roots this wallet will need
    $ nano-wallet work precompute               # spend the cores now
    ... the payment, later ...                  # no node asked for work

Three properties make the cache safe to read in the send path:

* **It is verified on the way out, not on the way in.** `get` recomputes the
  difficulty over the root the caller asked about and compares it against the
  threshold that caller needs. A file that was corrupted, edited by another
  process, or written for a different root is a MISS, not a bad block - so an
  untrusted cache directory can cost a wallet a cache hit and can never put
  an under-worked or foreign block on the network.
* **Work at the send threshold satisfies a receive too**, because the send
  threshold is the higher number. One entry per root serves every subtype,
  and `precompute` therefore always works at the send threshold.
* **It is inert unless configured.** With `NANO_WALLET_WORK_CACHE` unset,
  `from_env` returns None, nothing is read, nothing is written, and the send
  path behaves exactly as it did before this module existed.

Nothing here opens a socket, and reuse of a root's work enables nothing: two
blocks with the same previous hash are a fork, and a node rejects the second
whatever work it carries.
"""

import json
import os
import re
import stat
import tempfile
import time

import work as _work

#: Directory holding the cache. Unset means the whole feature is off.
CACHE_ENV = "NANO_WALLET_WORK_CACHE"

#: Roots kept at most, newest first. A root the account has moved past is
#: never asked about again, so old entries are dead weight rather than a
#: risk; this bounds the directory without needing to know which are dead.
MAX_ENTRIES = 64

#: Roots `want` remembers. One is enough for an account that sends in series;
#: a handful covers a wallet with several accounts.
MAX_PENDING = 16

PENDING_FILE = "pending.json"

#: Bytes read from one cache file at most. An entry is about 200 bytes; a file
#: larger than this is not one, and is a miss rather than something to load.
MAX_FILE_BYTES = 4096

#: Work exactly as a block carries it: 16 hex characters, nothing around them.
#: `bytes.fromhex` alone accepts surrounding whitespace, and work that
#: validates here but is published with that whitespace is rejected by every
#: node.
_WORK_RE = re.compile(r"\A[0-9a-fA-F]{16}\Z")


class CacheError(Exception):
    """A root or work value is malformed, or the cache cannot be used.

    Not the same thing as a miss.
    """


class CacheUnwritable(CacheError):
    """The cache directory cannot be created or written to."""


def _normalise_root(root_hex: str) -> str:
    """The 64 upper-case hex characters Nano writes a hash as, or raise."""
    if not isinstance(root_hex, str):
        raise CacheError("a root is 64 hex characters, got %r" % type(root_hex).__name__)
    text = root_hex.strip().upper()
    if len(text) != 64:
        raise CacheError("a root is 64 hex characters, got %d" % len(text))
    try:
        bytes.fromhex(text)
    except ValueError:
        raise CacheError("a root is 64 hex characters; %r is not hex" % root_hex) from None
    return text


def _normalise_work(work_hex):
    """The 16 lower-case hex characters a block carries, or None."""
    if not isinstance(work_hex, str) or not _WORK_RE.match(work_hex):
        return None
    return work_hex.lower()


def _read_small_json(path: str):
    """A cache file's JSON, or None. Never blocks and never loads much.

    Opened with O_NOFOLLOW (a symlink is a miss) and O_NONBLOCK (a FIFO
    cannot hang the send path waiting for a writer), then required to be a
    regular file, and read up to MAX_FILE_BYTES - so /dev/zero, a FIFO, a
    device or an oversized file all cost a cache miss and nothing else.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        data = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def threshold_for(subtype: str) -> int:
    """The work threshold a block of this subtype needs.

    An unrecognised or absent subtype gets the SEND threshold, which is the
    harder one: guessing low here would be the one mistake in this file that
    could put a block on the network under-worked.
    """
    if subtype in ("receive", "open"):
        return _work.RECEIVE_THRESHOLD
    return _work.SEND_THRESHOLD


class WorkCache(object):
    """A directory of verified proof-of-work, keyed by the root it is for."""

    def __init__(self, directory: str):
        self.directory = os.path.expanduser(directory)

    @classmethod
    def from_env(cls, env=None):
        """The cache the environment configures, or None when it configures none."""
        env = os.environ if env is None else env
        directory = (env.get(CACHE_ENV) or "").strip()
        if not directory:
            return None
        return cls(directory)

    # ------------------------------------------------------------- reading

    def path_for(self, root_hex: str) -> str:
        return os.path.join(self.directory, _normalise_root(root_hex) + ".json")

    def get(self, root_hex: str, threshold: int = None):
        """Work for `root_hex` that clears `threshold`, or None.

        Every failure is a miss: no directory, no file, unreadable JSON, a
        truncated write, work for another root, work that does not reach the
        threshold this caller needs. A miss costs the caller a node round
        trip; trusting any of those would cost it a rejected block.
        """
        if threshold is None:
            threshold = _work.SEND_THRESHOLD
        try:
            root = _normalise_root(root_hex)
        except CacheError:
            return None
        entry = _read_small_json(self.path_for(root))
        if not isinstance(entry, dict):
            return None
        found = _normalise_work(entry.get("work"))
        if found is None:
            return None
        # The root is re-derived from the KEY, not read from the file: a file
        # claiming a root it is not named for must not be able to answer for
        # the root the caller asked about.
        if not _work.validates(bytes.fromhex(root), found, threshold):
            return None
        return found

    def has(self, root_hex: str, threshold: int = None) -> bool:
        return self.get(root_hex, threshold) is not None

    def roots(self) -> list:
        """Every root the cache holds work for, newest write first."""
        try:
            names = os.listdir(self.directory)
        except OSError:
            return []
        entries = []
        for name in names:
            if not name.endswith(".json") or name == PENDING_FILE:
                continue
            try:
                root = _normalise_root(name[:-len(".json")])
            except CacheError:
                continue
            try:
                when = os.path.getmtime(os.path.join(self.directory, name))
            except OSError:
                continue
            entries.append((when, root))
        entries.sort(reverse=True)
        return [root for _, root in entries]

    # ------------------------------------------------------------- writing

    def put(self, root_hex: str, work_hex: str) -> dict:
        """Record work for a root, after checking it is work for that root.

        Refuses rather than writes when the work does not validate at all:
        the one thing a cache of proofs must never do is store something that
        is not a proof.
        """
        root = _normalise_root(root_hex)
        work_hex_in = work_hex
        work_hex = _normalise_work(work_hex)
        if work_hex is None:
            raise CacheError("work is exactly 16 hex characters, got %r" % (work_hex_in,))
        difficulty = _work.difficulty(bytes.fromhex(root), work_hex)
        if difficulty < _work.RECEIVE_THRESHOLD:
            raise CacheError(
                "work %s does not reach even the receive threshold for root %s "
                "(%#x < %#x)" % (work_hex, root, difficulty, _work.RECEIVE_THRESHOLD))
        entry = {
            "root": root,
            "work": work_hex,
            "difficulty": "%#x" % difficulty,
            "covers_send": difficulty >= _work.SEND_THRESHOLD,
            "found_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write_json(self.path_for(root), entry)
        self._evict()
        return entry

    def drop(self, root_hex: str) -> bool:
        """Forget a root's work. True if there was any."""
        try:
            os.unlink(self.path_for(_normalise_root(root_hex)))
        except OSError:
            return False
        return True

    # ------------------------------------------------- what to work on next

    def want(self, root_hex: str) -> None:
        """Remember that work for this root will be needed.

        Called with the hash of a block this wallet just published, which is
        the account's new frontier and therefore the root of its next block.
        Failures are swallowed on purpose: this is a hint for a later
        `precompute`, and a wallet that has already moved money must not
        raise because a hint could not be filed.
        """
        try:
            root = _normalise_root(root_hex)
        except CacheError:
            return
        try:
            pending = [root] + [r for r in self.pending() if r != root]
            self._write_json(os.path.join(self.directory, PENDING_FILE),
                             {"roots": pending[:MAX_PENDING]})
        except (OSError, CacheError):
            return

    def pending(self) -> list:
        """Roots that were wanted and have no SEND-grade work yet, newest first.

        This is the only thing that decides what is pending, and it decides it
        by looking: `put` does not cross the wanted list off, because work that
        only covers a receive does not make an account able to send, and a
        writer that crossed it off anyway would quietly leave the wallet
        unable to pay with a full-looking cache. The file is pruned as a side
        effect of the next `want`, which rewrites it from this list.
        """
        stored = _read_small_json(os.path.join(self.directory, PENDING_FILE))
        roots = stored.get("roots") if isinstance(stored, dict) else None
        if not isinstance(roots, list):
            return []
        out = []
        for item in roots:
            try:
                root = _normalise_root(item)
            except CacheError:
                continue
            if root not in out and not self.has(root, _work.SEND_THRESHOLD):
                out.append(root)
        return out

    def precompute(self, root_hex: str = None, threshold: int = None,
                   budget_seconds: float = None, workers: int = None) -> dict:
        """Find and store work for one root, at the send threshold by default.

        Returns what was done. Raises `work.WorkUnavailable` when the budget
        ran out, which is the honest answer on a host too slow for a send:
        nothing is stored and the wallet is exactly where it was.
        """
        if threshold is None:
            threshold = _work.SEND_THRESHOLD
        if budget_seconds is None:
            budget_seconds = _work.DEFAULT_PARALLEL_BUDGET_SECONDS
        if root_hex is None:
            waiting = self.pending()
            if not waiting:
                return {"precomputed": None, "reason": "nothing is pending"}
            root_hex = waiting[0]
        root = _normalise_root(root_hex)
        existing = self.get(root, threshold)
        if existing is not None:
            return {"precomputed": root, "work": existing, "already_had_it": True}
        # Before the search, not after it: a directory the result cannot be
        # written to must cost the caller nothing, not minutes of every core.
        self.check_writable()
        started = time.monotonic()
        found = _work.solve_parallel(bytes.fromhex(root), threshold,
                                     budget_seconds=budget_seconds, workers=workers)
        entry = self.put(root, found)
        entry["seconds"] = round(time.monotonic() - started, 3)
        entry["workers"] = _work.worker_count(workers)
        entry["already_had_it"] = False
        entry["precomputed"] = root
        return entry

    def check_writable(self) -> None:
        """Raise CacheUnwritable unless a file can be created in the cache."""
        try:
            os.makedirs(self.directory, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=".probe-", dir=self.directory)
            os.close(handle)
            os.unlink(temporary)
        except OSError as exc:
            raise CacheUnwritable("cannot write to the work cache at %s: %s"
                                  % (self.directory, exc)) from None

    # ------------------------------------------------------------ internals

    def _write_json(self, path: str, payload: dict) -> None:
        """Write a whole file or none of it, so a reader never sees a half one."""
        directory = os.path.dirname(path)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise CacheUnwritable("cannot create the work cache at %s: %s"
                                  % (directory, exc)) from None
        temporary = None
        try:
            handle, temporary = tempfile.mkstemp(prefix=".work-", dir=directory)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
        except OSError as exc:
            raise CacheUnwritable("cannot write to the work cache at %s: %s"
                                  % (directory, exc)) from None
        finally:
            if temporary is not None and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _evict(self) -> None:
        for root in self.roots()[MAX_ENTRIES:]:
            self.drop(root)
