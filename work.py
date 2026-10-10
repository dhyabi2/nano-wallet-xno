"""Proof-of-work for a block, found locally when the node will not do it.

Public nodes refuse `work_generate`. That is the whole of why a brand-new
wallet could not receive: it has no XNO yet, so its first operation is a
receive, and the only node it has is a public one. Nano asks for much less
work from a receive or open block than from a send - the thresholds below
differ by a factor of 64 - and the receive threshold is reachable here, in
Python, in seconds.

The send threshold is not reachable in seconds, and the split this module
draws is about WHEN the work is done rather than whether it can be:

* `solve` is what the send path calls, and it still refuses any threshold
  above a receive's. A wallet that silently burns minutes of CPU between
  "pay this" and the block going out is worse than one that says the node
  owes it work, so nothing inline ever attempts a send.
* `solve_parallel` is what an agent calls AHEAD of the payment, on every
  core, for a root it already knows - its own frontier, which is fixed the
  moment its last block confirms. See `prework.py`: work found then is read
  from a file at pay time, in microseconds, and the send needs no node at
  all. `estimate_seconds` says what that costs on this machine before
  anything is spent.

Validity, per the protocol: blake2b-64 over the 8 work bytes in REVERSE
order followed by the root, read as a little-endian unsigned integer, must
be at least the threshold. The root is the account's public key for an open
block and the previous block's hash otherwise (`blocks.work_root`).

Checked against a real open block published on the live network -
`BCE621224274F7DCB1B9BFB212CF9E5CADC50B47E8BEA5C2E4ED0F063566D85B`, the one
`tests/test_work.py` pins. Its work clears RECEIVE_THRESHOLD and fails
SEND_THRESHOLD, which is what a receive block's work should do, and no other
byte order validates it at all.
"""

import hashlib
import os
import queue as _queue
import time

# Nano's epoch-2 thresholds. A send or a change needs 64x the work of a
# receive or an open. Never lower either of these.
SEND_THRESHOLD = 0xFFFFFFF800000000
RECEIVE_THRESHOLD = 0xFFFFFE0000000000

# Subtypes whose work this module will look for locally. Everything that
# spends is absent on purpose - at the measured rate a send takes minutes,
# and a wallet that quietly burns six minutes of CPU before spending is
# worse than one that says the node owes it work.
LOCAL_SUBTYPES = frozenset({"receive", "open"})

# A receive needs 2**23 hashes on average. The default budget is a wall
# clock, not a count, so the bound holds on a slow machine too; at the
# ~1.4M hashes a second this manages it is about ten times the average,
# so giving up is rare and never silent - the caller gets `work_unavailable`.
DEFAULT_BUDGET_SECONDS = 60.0


class WorkUnavailable(Exception):
    """No valid work was found inside the budget."""


def difficulty(root: bytes, work_hex: str) -> int:
    """The work's difficulty, to compare against a threshold."""
    work = bytes.fromhex(work_hex)
    if len(work) != 8:
        raise ValueError("work must be 8 bytes (16 hex characters), got %d" % len(work))
    if len(root) != 32:
        raise ValueError("root must be 32 bytes, got %d" % len(root))
    digest = hashlib.blake2b(work[::-1] + root, digest_size=8).digest()
    return int.from_bytes(digest, "little")


def validates(root: bytes, work_hex: str, threshold: int = RECEIVE_THRESHOLD) -> bool:
    """Whether `work_hex` is valid work for `root` at `threshold`."""
    try:
        return difficulty(root, work_hex) >= threshold
    except ValueError:
        return False


def solve(root: bytes, threshold: int = RECEIVE_THRESHOLD,
          budget_seconds: float = DEFAULT_BUDGET_SECONDS, _now=time.monotonic) -> str:
    """Work for `root` at `threshold`, as the 16 hex characters a block carries.

    Refuses the send threshold outright rather than spending minutes on it:
    a caller that reaches here for a send has a node that owes it work, or
    work it should have found before the payment (`prework.py`), and should
    be told so while the block is still unsigned.
    """
    if len(root) != 32:
        raise ValueError("root must be 32 bytes, got %d" % len(root))
    if threshold > RECEIVE_THRESHOLD:
        raise WorkUnavailable(
            "local work is only attempted at the receive threshold (%#x); %#x "
            "needs a node that generates work" % (RECEIVE_THRESHOLD, threshold))
    return _search(root, threshold, budget_seconds, _now)


def _search(root: bytes, threshold: int, budget_seconds: float, _now=time.monotonic) -> str:
    """The search itself, with no policy about which thresholds are allowed.

    Separate from `solve` so that `solve`'s refusal of the send threshold
    stays the one place that decides it. Nothing calls this with a send
    threshold except `solve_parallel`'s own workers and its single-process
    fallback, both of which are reached only from `prework.py` - ahead of a
    payment, never inside one.
    """
    blake2b = hashlib.blake2b                      # bound once; this is the hot loop
    nonce = int.from_bytes(os.urandom(8), "little")
    deadline = _now() + budget_seconds
    attempts = 0
    while True:
        # Checked before the first hash as well as every 8192 after it, so an
        # exhausted budget gives up without doing work - which is what makes
        # this branch testable at all, rather than a 1-in-1000 race.
        if attempts % 8192 == 0 and _now() >= deadline:
            raise WorkUnavailable(
                "no proof-of-work found for this block in %.0f seconds (%d attempts); "
                "use a node that generates work, or set a work peer"
                % (budget_seconds, attempts))
        candidate = nonce.to_bytes(8, "little")
        if int.from_bytes(blake2b(candidate + root, digest_size=8).digest(),
                          "little") >= threshold:
            # The bytes hashed are the work in reverse, so the block carries
            # the reverse of what was hashed.
            return candidate[::-1].hex()
        nonce = (nonce + 1) & 0xFFFFFFFFFFFFFFFF
        attempts += 1


# ------------------------------------------------------- what it costs, measured

def expected_attempts(threshold: int = RECEIVE_THRESHOLD) -> float:
    """Hashes a search averages at `threshold`.

    Each hash is uniform over 2**64, so clearing the threshold is a
    geometric trial with success probability (2**64 - threshold) / 2**64 and
    the mean is its reciprocal. A threshold of 0 succeeds on the first hash.
    """
    space = 1 << 64
    if not 0 <= threshold < space:
        raise ValueError("threshold must be a 64-bit unsigned integer, got %#x" % threshold)
    return space / float(space - threshold)


def measure_rate(seconds: float = 0.25, _now=time.monotonic) -> float:
    """Hashes a second this machine manages, measured rather than assumed.

    The figure a README quotes was measured on one machine; an agent's host
    is not that machine, and the honest way to tell an operator what a send
    will cost is to time the loop it will actually run. Hashes a fixed-size
    batch repeatedly so the clock is read once per batch, not once per hash.
    """
    if seconds <= 0:
        raise ValueError("seconds must be positive, got %r" % seconds)
    blake2b = hashlib.blake2b
    root = os.urandom(32)
    batch = 20000
    hashed = 0
    start = _now()
    deadline = start + seconds
    while True:
        nonce = int.from_bytes(os.urandom(8), "little")
        for _ in range(batch):
            blake2b(nonce.to_bytes(8, "little") + root, digest_size=8).digest()
            nonce = (nonce + 1) & 0xFFFFFFFFFFFFFFFF
        hashed += batch
        now = _now()
        if now >= deadline:
            break
    elapsed = now - start
    if elapsed <= 0:
        raise ValueError("the clock did not advance over %d hashes" % hashed)
    return hashed / elapsed


def estimate_seconds(threshold: int = SEND_THRESHOLD, rate: float = None,
                     workers: int = None) -> float:
    """How long work at `threshold` averages here, in seconds.

    An average, not a bound: the search is memoryless, so the spread around
    this is wide - about a third of searches take longer than the mean and
    one in twenty takes three times it. A caller that budgets exactly this
    will give up on roughly a third of its payments.
    """
    if rate is None:
        rate = measure_rate()
    if rate <= 0:
        raise ValueError("rate must be positive, got %r" % rate)
    return expected_attempts(threshold) / (rate * float(worker_count(workers)))


def worker_count(workers: int = None) -> int:
    """How many processes a parallel search will use."""
    if workers is None:
        return max(1, os.cpu_count() or 1)
    workers = int(workers)
    if workers < 1:
        raise ValueError("workers must be at least 1, got %d" % workers)
    return workers


# ------------------------------------------------- the search, on every core

# Half an hour. A send averages about 500 seconds of one core here, so this
# is roughly four core-hours of headroom on a single-core host and gives up
# rather than running until the machine is rebooted. It is a wall clock, so
# it holds on a slow machine too.
DEFAULT_PARALLEL_BUDGET_SECONDS = 1800.0


def _worker(root: bytes, threshold: int, budget_seconds: float, stop, found) -> None:
    """One process's share of the search. Stops on a find, a sibling's find, or the budget.

    Each worker starts at its own random nonce rather than at a stride of a
    shared one: the search space is 2**64 and a send needs about 2**29
    hashes, so the chance that two workers ever walk the same nonce is
    negligible, and nothing has to be partitioned or communicated to make
    that true.
    """
    blake2b = hashlib.blake2b
    nonce = int.from_bytes(os.urandom(8), "little")
    deadline = time.monotonic() + budget_seconds
    attempts = 0
    while True:
        if attempts % 8192 == 0:
            if stop.is_set() or time.monotonic() >= deadline:
                return
        candidate = nonce.to_bytes(8, "little")
        if int.from_bytes(blake2b(candidate + root, digest_size=8).digest(),
                          "little") >= threshold:
            found.put(candidate[::-1].hex())
            stop.set()
            return
        nonce = (nonce + 1) & 0xFFFFFFFFFFFFFFFF
        attempts += 1


def _fork_context():
    """A `fork` multiprocessing context, or None where there is no fork.

    Only `fork` is used, never `spawn`. A spawning child re-imports the
    parent's `__main__`, and this package's `__main__.py` calls `main()` at
    module level - under `spawn` that would re-run the command in every
    worker. Where fork is absent (Windows) `solve_parallel` searches in this
    process instead and says so by being slower, which is the safe failure.
    """
    # Imported here rather than at the top of the file so that this module's
    # import graph holds nothing but offline stdlib: `e2e_check.py` walks that
    # graph transitively and `multiprocessing` reaches `socket` through it.
    # Nothing here uses a socket - a forked worker talks over a pipe.
    import multiprocessing

    try:
        return multiprocessing.get_context("fork")
    except (ValueError, AttributeError):
        return None


def solve_parallel(root: bytes, threshold: int = RECEIVE_THRESHOLD,
                   budget_seconds: float = DEFAULT_PARALLEL_BUDGET_SECONDS,
                   workers: int = None, _context=None) -> str:
    """Work for `root` at `threshold`, searched on every core at once.

    This is the one entry point that will attempt a send threshold, and it
    is not reachable from the send path: `prework.py` calls it ahead of a
    payment, for a root the agent already knows. The answer is verified in
    this process before it is returned, so a worker that somehow reported
    the wrong nonce raises `WorkUnavailable` instead of putting an
    under-worked block on the network.
    """
    if len(root) != 32:
        raise ValueError("root must be 32 bytes, got %d" % len(root))
    expected_attempts(threshold)                   # rejects a threshold out of range
    count = worker_count(workers)
    context = _context if _context is not None else _fork_context()
    if context is None or count == 1:
        # One core, or a platform with no fork: the same search, in process.
        return _search(root, threshold, budget_seconds)

    stop = context.Event()
    found = context.Queue()
    started = []
    try:
        for _ in range(count):
            process = context.Process(target=_worker,
                                      args=(root, threshold, budget_seconds, stop, found))
            process.daemon = True
            process.start()
            started.append(process)
        try:
            # A worker gives up at its own deadline, so waiting a little past
            # the budget collects a find made in its last moments rather than
            # calling it a timeout.
            answer = found.get(timeout=budget_seconds + 5.0)
        except _queue.Empty:
            answer = None
    finally:
        stop.set()
        for process in started:
            process.join(timeout=5.0)
            if process.is_alive():                 # pragma: no cover - a wedged child
                process.terminate()
                process.join(timeout=5.0)
    if answer is None:
        raise WorkUnavailable(
            "no proof-of-work found for %s at %#x in %.0f seconds on %d worker(s); "
            "raise the budget, or use a node that generates work"
            % (root.hex().upper(), threshold, budget_seconds, count))
    if not validates(root, answer, threshold):
        raise WorkUnavailable(
            "a worker reported work that does not validate for this root at %#x; "
            "nothing was used" % threshold)
    return answer
