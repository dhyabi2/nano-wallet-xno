"""nano-pay402 - buy from an x402 seller that has priced XNO. Dry-run by default.

    nano-pay402 https://seller.example/api/audit                      # DRY RUN: show what would be paid
    nano-pay402 URL --max-xno 0.1                                     # dry run, also checks the cap
    nano-pay402 URL --pay --key-file ~/.nano/key --max-xno 0.1        # really pay (needs a funded key file)
    nano-pay402 URL --pin-file ~/.nano/payto-pins.json                # refuse if payTo changed since the last read

What it does, in order, and stops at the first thing that is wrong:

  1. Fetches URL and expects HTTP 402 with an x402 `accepts[]` (body JSON, or the base64
     `payment-required` header). Redirects are NOT followed: a 402 from another host than the
     one you named is not a price you agreed to.
  2. Picks the `nano:mainnet` / `nano-mainnet` entry. Refuses if there is none.
  3. Checks `payTo` against its checksum and the price as an integer count of raw (no floats).
  3b. With --pin-file, compares payTo with the one this seller's origin (scheme+host+port) named
     the first time it was read; a different payTo is refused as `payto-changed`. The first read
     records it. Without --pin-file nothing is compared. The directory holding the pin file must
     already exist (it is not created; a missing one is refused as `pin-file-unwritable` and
     leaves nothing behind). An empty pin file (e.g. after a crash) holds no pins and is refused as
     `pin-file-malformed`: remove it. A pin file that is not a regular file (a FIFO, device...)
     is refused as `pin-file-unreadable`.
  4. Compares the price with --max-xno. Paying REQUIRES --max-xno: an unbounded buyer is not built.
  5. Without --pay it prints that plan as JSON and exits 0. Nothing was signed, no key was read.
  6. With --pay it signs ONE send block locally with your key file (the key never leaves this
     process; only the signed block goes to the node), waits for confirmation, then retries the
     request with the `X-PAYMENT` header. If the seller asks for a `nano-block-sig/1` binding
     (dual-rail does), it adds the signature that proves you made the payment.
  7. Prints the block hash and the seller's reply as JSON.

Safe to re-run after a failure: the send is idempotent per (url, payTo, amount) through a small
ledger file next to the key file, so a retry resubmits the SAME block instead of paying twice.
Exit codes: 0 ok, 1 refused before paying, 2 usage, 3 paid but the seller did not accept it.
"""
import base64
import contextlib
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:                                     # pragma: no cover - not POSIX
    fcntl = None

import ed25519_blake2b as _ed
import keystore as _keystore
import nanoaddr
import nanonode
import payments
import wallet

USER_AGENT = "nano-pay402/1.0 (+https://github.com/dhyabi2/nano-wallet-xno)"
DEFAULT_NODE = "https://rpc.nano.to"
NANO_NETWORKS = ("nano:mainnet", "nano-mainnet")
BINDING_SCHEME = "nano-block-sig/1"
BINDING_PREFIX = b"nano-402-binding/1\n"
MAX_BODY = 1 << 20


class Refused(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason, self.message = reason, message


# ------------------------------------------------------------------ fetch

def fetch(url, method="GET", data=None, headers=None, timeout=20):
    """(status, headers dict lower-cased, body bytes). Never follows a redirect.

    The HTTP call itself lives in nanonode.http_fetch: nanonode.py is the one module
    in this wallet that touches the network (e2e_check.py proves it)."""
    try:
        return nanonode.http_fetch(url, method=method, data=data,
                                   headers=dict({"User-Agent": USER_AGENT}, **(headers or {})),
                                   timeout=timeout, max_body=MAX_BODY)
    except nanonode.Unreachable as exc:
        raise Refused("unreachable", "could not reach %s: %s" % (url, exc)) from None


# --------------------------------------------------------------- the entry

def read_accepts(status, headers, body):
    if status != 402:
        raise Refused("not_a_402", "expected HTTP 402, the server answered %d" % status)
    documents = []
    try:
        documents.append(json.loads(body.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        pass
    header = headers.get("payment-required")
    if header:
        try:
            documents.append(json.loads(base64.b64decode(header, validate=True).decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            pass
    for doc in documents:
        if isinstance(doc, dict) and isinstance(doc.get("accepts"), list):
            return doc["accepts"]
    raise Refused("no_accepts", "the 402 carries no accepts[] array in its body or payment-required header")


def pick_nano(accepts):
    for entry in accepts:
        if (isinstance(entry, dict) and entry.get("network") in NANO_NETWORKS
                and entry.get("scheme", "exact") == "exact"):
            return entry
    raise Refused("no_nano_entry", "no nano:mainnet entry in accepts[] (%d other entr%s)"
                  % (len(accepts), "y" if len(accepts) == 1 else "ies"))


def entry_amount_raw(entry):
    for field in ("amount", "maxAmountRequired", "max_amount_required"):
        value = entry.get(field)
        if value is None:
            continue
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,40}", value) and int(value) > 0:
            return int(value)
        raise Refused("bad_amount", "%s=%r is not a positive integer count of raw" % (field, value))
    raise Refused("bad_amount", "the nano entry names no amount")


def resource_key(path):
    """The seller-side canonical resource (dual-rail's resource_key): drop query, collapse
    slashes, drop a trailing slash, lowercase ASCII."""
    text = (path or "").split("?", 1)[0].split("#", 1)[0]
    text = re.sub(r"/{2,}", "/", "/" + text)
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def plan(url, accepts, max_xno=None):
    entry = pick_nano(accepts)
    pay_to = entry.get("payTo")
    verdict = nanoaddr.validate(pay_to) if isinstance(pay_to, str) else {"valid": False, "reason": "missing"}
    if not verdict["valid"]:
        raise Refused("bad_pay_to", "payTo %r fails its checksum (%s): paying it would lose the money"
                      % (pay_to, verdict.get("reason")))
    raw = entry_amount_raw(entry)
    binding = (entry.get("extra") or {}).get("binding")
    out = {"url": url, "network": entry["network"], "asset": entry.get("asset"),
           "pay_to": verdict["normalised"], "amount_raw": str(raw),
           "amount_xno": wallet.raw_to_xno(raw), "binding": None}
    if binding is not None:
        if not isinstance(binding, dict) or binding.get("scheme") != BINDING_SCHEME:
            raise Refused("binding_scheme_unsupported",
                          "the seller requires a payment binding this tool cannot make (%r): not paying"
                          % (binding.get("scheme") if isinstance(binding, dict) else binding))
        wanted = resource_key(nanonode.url_path(url))
        if binding.get("resource") != wanted:
            raise Refused("binding_resource_mismatch",
                          "the seller's binding is for resource %r but you asked for %r: not paying"
                          % (binding.get("resource"), wanted))
        out["binding"] = BINDING_SCHEME
    if max_xno is not None:
        try:
            cap = wallet.xno_to_raw(max_xno)
        except ValueError as exc:
            raise Refused("bad_cap", "--max-xno %r: %s" % (max_xno, exc)) from None
        out["max_xno"] = max_xno
        out["within_cap"] = raw <= cap
        if raw > cap:
            raise Refused("over_cap", "the seller asks %s XNO, more than --max-xno %s: not paying"
                          % (out["amount_xno"], max_xno))
    return out, entry


# ------------------------------------------------------------- payTo pin

def check_pin(pin_path, url, pay_to, now=time.time):
    """Pin payTo per seller origin. The first read records {origin: {pay_to, first_seen}} in
    pin_path; a later read of the same origin naming another payTo raises Refused('payto-changed').
    Returns the pin record. The file is only written on first sight of an origin.

    Fails closed: a missing pin file is the only "no pins yet". A pin file that cannot be read,
    is a symlink, is not JSON, repeats a key, is not a JSON object, or holds an entry that is not
    {"pay_to": "<non-empty>", "first_seen": "<non-empty>", ...} is refused (pin-file-unreadable / pin-file-malformed / pin-entry-malformed) and is NOT
    rewritten (an empty file says so and names the path to remove; it is never auto-deleted), so a damaged file never silently loses the other sellers' pins. The
    read-check-write runs under an exclusive flock on `pin_path + ".lock"` (POSIX; where fcntl
    does not exist the lock is skipped and concurrent writers can still race)."""
    try:
        origin = nanonode.url_origin(url)
    except ValueError as exc:
        raise Refused("usage", "cannot take the origin of %r: %s" % (url, exc)) from None
    if "\0" in pin_path:
        raise Refused("pin-file-unreadable", "pin file path %r contains a NUL byte: not paying"
                      % pin_path)
    directory = os.path.dirname(os.path.abspath(pin_path))
    if not os.path.isdir(directory):
        # Checked before the lock: opening FILE.lock would otherwise be the first thing to touch
        # the path, and nothing is created for a pin file that cannot be kept.
        raise Refused("pin-file-unwritable", "the directory %s for pin file %s does not exist; "
                      "create it first (nothing was created): not paying" % (directory, pin_path))
    with _file_lock(pin_path):
        if os.path.islink(pin_path):
            # A dangling link would read as "no pins yet" (every seller trusted fresh) and the
            # save would replace a live link with a plain file: refuse any link outright.
            raise Refused("pin-file-unreadable", "pin file %s is a symlink; point --pin-file at "
                          "the real file: not paying" % pin_path)
        pins = _strict_load(pin_path, "pin-file", nofollow=True)
        for key, value in pins.items():
            if not (isinstance(value, dict)
                    and isinstance(value.get("pay_to"), str) and value["pay_to"]
                    and isinstance(value.get("first_seen"), str) and value["first_seen"]):
                raise Refused("pin-entry-malformed",
                              "pin file %s: the entry for %r is not {\"pay_to\": ..., "
                              "\"first_seen\": ...}; fix or remove it by hand: not paying"
                              % (pin_path, key))
        pinned = pins.get(origin)
        if pinned is not None:
            if pinned["pay_to"] != pay_to:
                raise Refused("payto-changed",
                              "%s named payTo %s when first read (%s), now %s: not paying. If the "
                              "seller really moved, remove its entry from %s"
                              % (origin, pinned["pay_to"], pinned["first_seen"], pay_to, pin_path))
            return dict(pinned, origin=origin, pinned_now=False)
        record = {"pay_to": pay_to,
                  "first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))}
        pins[origin] = record
        try:
            _ledger_save(pin_path, pins)
        except OSError as exc:
            raise Refused("pin-file-unwritable", "cannot write pin file %s: %s" % (pin_path, exc)) from None
        return dict(record, origin=origin, pinned_now=True)


# ------------------------------------------------------------- binding sig

def binding_message(block_hash, nonce, resource, pay_to):
    canonical = nanoaddr.encode(nanoaddr.decode(pay_to), "nano_")
    return (BINDING_PREFIX + bytes.fromhex(block_hash) + b"\n" + nonce.encode("utf-8") + b"\n"
            + resource.encode("utf-8") + b"\n" + canonical.encode("utf-8"))


def binding_signature(private_key, block_hash, nonce, resource, pay_to):
    public_key = _ed.public_key_from_private(private_key)
    return _ed.sign(binding_message(block_hash, nonce, resource, pay_to), private_key,
                    public_key).hex().upper()


def payment_header(block_hash, entry, private_key=None):
    payload = {"blockHash": block_hash}
    binding = (entry.get("extra") or {}).get("binding")
    if binding:
        nonce, resource = binding["nonce"], binding["resource"]
        payload["binding"] = {"nonce": nonce, "signature": binding_signature(
            private_key, block_hash, nonce, resource, entry["payTo"])}
    document = {"x402Version": 1, "scheme": "exact", "network": "nano:mainnet", "payload": payload}
    return base64.b64encode(json.dumps(document).encode("utf-8")).decode("ascii")


# ------------------------------------------------------------------- pay

def _no_duplicate_keys(pairs):
    """object_pairs_hook: a repeated key would let the last value silently win."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key %r" % key)
        out[key] = value
    return out


def _strict_load(path, what, nofollow=False):
    """The JSON object at path; {} ONLY when the file does not exist (for the sent ledger, not
    when the path is a dangling symlink, and every ledger entry must itself be an object). Anything else that is
    wrong (unreadable, a directory, not JSON, a repeated key, too deeply nested, not an object)
    raises Refused('<what>-unreadable' or '<what>-malformed'): an empty reading of a damaged file
    would forget what it held. With nofollow the open uses O_NOFOLLOW (where the OS has it), so
    a symlink swapped in after an islink() check is refused (ELOOP) rather than followed."""
    # O_NONBLOCK so a FIFO opens at once instead of blocking (while FILE.lock is held) until a
    # writer appears; the fstat below then refuses anything that is not a regular file.
    flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
             | (getattr(os, "O_NOFOLLOW", 0) if nofollow else 0))
    # The sent ledger records blocks already sent: deleting it could make a retry sign a NEW
    # block and pay twice, so its advice never says remove. The pin file can be removed.
    if what == "ledger":
        advice = ("restore it from a backup or fix it by hand; do NOT delete it - it records "
                  "blocks already sent")
    else:
        advice = "fix or remove it by hand"
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Refused(what + "-unreadable", "%s is not a regular file (a FIFO, device, "
                              "socket or directory?): not paying" % path)
            handle = os.fdopen(fd, "r", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            text = handle.read()
    except FileNotFoundError:
        if what == "ledger" and os.path.islink(path):
            try:
                target = os.readlink(path)
            except OSError:
                target = "?"
            # A dangling symlink is not "no ledger yet": reading it as empty would sign a new
            # block, and the save afterwards would replace the link, so a retry could pay twice.
            raise Refused(what + "-unreadable", "sent ledger %s is a dangling symlink (it points "
                          "to %s, which does not exist), so pay402 cannot tell which blocks were "
                          "already sent; point the link at the real ledger or restore it from a "
                          "backup; do NOT delete it: not paying"
                          % (path, target)) from None
        return {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # ValueError: a path with an embedded NUL byte.
        raise Refused(what + "-unreadable", "cannot read %s: %s: not paying" % (path, exc)) from None
    if not text.strip():
        if what == "pin-file":
            # An empty pin file (e.g. after a crash) holds no pins but is not "no pins yet":
            # it is not deleted here; say so plainly and let the user remove it.
            raise Refused(what + "-malformed", "pin file %s is empty (0 bytes or only whitespace), "
                          "so it is corrupt and holds no pins; if you have no pins to keep, remove "
                          "it (rm %s) and run again: not paying" % (path, path))
        # An empty sent ledger is usually a crash before the first write, but it may also be a
        # truncated one that lost blocks already sent. Name the exact recovery: only the user
        # knows whether a payment was recorded in it.
        raise Refused(what + "-malformed", "sent ledger %s is empty (0 bytes or only whitespace), "
                      "so it is corrupt and pay402 cannot tell which blocks were already sent; if "
                      "you are sure no payment was recorded in it, write {} into it (printf '{}' > "
                      "%s) and run again; otherwise restore it from a backup; do NOT delete it - "
                      "an absent ledger could make a retry pay twice: not paying"
                      % (path, shlex.quote(path)))
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError) as exc:
        raise Refused(what + "-malformed", "%s is not valid JSON (%s): %s: not paying"
                      % (path, exc, advice)) from None
    if not isinstance(data, dict):
        raise Refused(what + "-malformed", "%s holds a JSON %s, not an object: %s: not paying"
                      % (path, type(data).__name__, advice))
    if what == "ledger":
        # Every entry is one payment's record; payments.send reads each one as an object.
        for key, record in data.items():
            if not isinstance(record, dict):
                raise Refused(what + "-malformed", "%s entry %r holds a JSON %s, not an object: "
                              "%s: not paying" % (path, key, type(record).__name__, advice))
    return data


def _ledger_load(path):
    """The sent ledger. Same fail-closed rule as the pin file: an unreadable or corrupt ledger
    read as empty would forget a block already sent and could pay twice."""
    return _strict_load(path, "ledger")


def _ledger_directory_check(path):
    """Refuse before signing when the sent ledger could not be saved: a payment published and
    then not recorded could make a retry sign a NEW block and pay twice."""
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        raise Refused("ledger-unwritable", "the directory %s for sent ledger %s does not exist "
                      "(or is not a directory), so a payment could not be recorded; create it "
                      "first, or if the ledger was moved point --ledger at it; do NOT start a new "
                      "empty ledger elsewhere if payments were already recorded (nothing was "
                      "created): not paying" % (directory, path))
    if not os.access(directory, os.W_OK | os.X_OK):
        raise Refused("ledger-unwritable", "the directory %s for sent ledger %s is not writable, "
                      "so a payment could not be recorded; fix its permissions; do NOT start a "
                      "new empty ledger elsewhere if payments were already recorded: not paying"
                      % (directory, path))


@contextlib.contextmanager
def _file_lock(path):
    """Exclusive flock on path + '.lock' (POSIX). Without fcntl it is a no-op."""
    if fcntl is None:
        yield
        return
    try:
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise Refused("pin-file-unwritable", "cannot open lock file %s.lock: %s" % (path, exc)) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _ledger_save(path, sent):
    """Atomic replace through a unique temp file in the same directory (mkstemp: O_EXCL, 0600,
    so a planted symlink at a predictable name is never followed)."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(sent, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def idempotency_key(url, pay_to, raw):
    return "pay402-" + hashlib.sha256(("%s|%s|%d" % (url, pay_to, raw)).encode()).hexdigest()[:40]


def wait_confirmed(node, block_hash, seconds=30, sleep=time.sleep):
    deadline = time.time() + seconds
    while True:
        try:
            if (node.block_info(block_hash) or {}).get("confirmed") is True:
                return True
        except nanonode.NodeError:
            pass
        if time.time() >= deadline:
            return False
        sleep(1)


def pay(url, entry, summary, key_file, max_xno, node, method="GET", data=None,
        ledger=None, fetcher=fetch, sleep=time.sleep, confirm_seconds=30):
    keys = _keystore.KeyStore()
    try:
        source = keys.load_file(key_file)
    except _keystore.KeyStoreError as exc:
        raise Refused(exc.reason, exc.message) from None
    ledger = ledger or os.path.expanduser(key_file) + ".pay402-sent.json"
    _ledger_directory_check(ledger)
    sent = _ledger_load(ledger)
    env = {"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_MAX_SEND_XNO": max_xno}
    key = idempotency_key(url, summary["pay_to"], int(summary["amount_raw"]))
    try:
        result = payments.send(source, summary["pay_to"], summary["amount_xno"], key, node, keys,
                               sent, env=env)
    except payments.ToolError as exc:
        _ledger_save(ledger, sent)
        raise Refused(exc.reason, exc.message) from None
    _ledger_save(ledger, sent)
    block_hash = result["block_hash"]
    confirmed = wait_confirmed(node, block_hash, confirm_seconds, sleep)
    header = payment_header(block_hash, entry, keys.get(source))
    status, _, body = fetcher(url, method=method, data=data,
                              headers={"X-PAYMENT": header, "Content-Type": "application/json"}
                              if data else {"X-PAYMENT": header})
    return {"paid": True, "block_hash": block_hash, "from": source, "to": summary["pay_to"],
            "amount_xno": summary["amount_xno"], "confirmed": confirmed,
            "replayed_send": bool(result.get("replayed")),
            "receipt": "https://nanolooker.com/block/%s" % block_hash,
            "seller_status": status, "seller_body": body.decode("utf-8", "replace")[:4000]}


# ------------------------------------------------------------------- cli

def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 >= len(args):
            raise Refused("usage", "%s needs a value" % name)
        value = args[i + 1]
        del args[i:i + 2]
        return value
    return default


def main(argv=None, fetcher=fetch, node=None, sleep=time.sleep):
    args = list(sys.argv[1:] if argv is None else argv)
    out = lambda obj: print(json.dumps(obj, indent=2, sort_keys=True))  # noqa: E731
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    try:
        do_pay = "--pay" in args
        if do_pay:
            args.remove("--pay")
        max_xno = _opt(args, "--max-xno")
        key_file = _opt(args, "--key-file")
        node_url = _opt(args, "--node", os.environ.get("NANO_NODE_URL") or DEFAULT_NODE)
        method = _opt(args, "--method", "GET").upper()
        data = _opt(args, "--data")
        ledger = _opt(args, "--ledger")
        pin_file = _opt(args, "--pin-file")
        if pin_file is not None and not pin_file.strip():
            raise Refused("usage", "--pin-file needs a file path, got %r: an empty path would "
                          "silently turn pinning off" % pin_file)
        if len(args) != 1 or args[0].startswith("--"):
            raise Refused("usage", "expected exactly one URL, got %r" % args)
        url = args[0]
        if nanonode.url_scheme(url) not in ("http", "https"):
            raise Refused("usage", "URL must be http or https")
        body = data.encode("utf-8") if data is not None else None
        status, headers, response = fetcher(url, method=method, data=body,
                                            headers={"Content-Type": "application/json"} if body else {})
        accepts = read_accepts(status, headers, response)
        summary, entry = plan(url, accepts, max_xno)
        if pin_file is not None:
            pin = check_pin(os.path.expanduser(pin_file), url, summary["pay_to"])
            summary["pin"] = {"origin": pin["origin"], "pay_to": pin["pay_to"],
                              "first_seen": pin["first_seen"], "pinned_now": pin["pinned_now"]}
        if not do_pay:
            summary["dry_run"] = True
            summary["next"] = ("add --pay --key-file FILE --max-xno %s to pay" % summary["amount_xno"]
                               if max_xno is None else "add --pay --key-file FILE to pay")
            out(summary)
            return 0
        if max_xno is None:
            raise Refused("cap_required", "paying requires --max-xno: an unbounded buyer is not built")
        if not key_file:
            raise Refused("key_required", "paying requires --key-file (a file holding your 64-hex private key, "
                          "mode 600; make one with `python3 nano_keygen.py new --key-file FILE`)")
        node = node or nanonode.HttpNanoNode(node_url)
        result = pay(url, entry, summary, key_file, max_xno, node, method, body, ledger, fetcher, sleep)
        if "pin" in summary:
            result["pin"] = summary["pin"]
        out(result)
        return 0 if 200 <= result["seller_status"] < 300 else 3
    except Refused as exc:
        out({"paid": False, "refused": exc.reason, "message": exc.message})
        return 2 if exc.reason == "usage" else 1


if __name__ == "__main__":
    sys.exit(main())
