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
Each payment's load -> send -> save of that ledger runs under an exclusive flock on
LEDGER.lock (POSIX): a second run on the same ledger waits for the first, then sees its record.
Exit codes: 0 ok, 1 refused before paying, 2 usage, 3 paid but the seller did not accept it,
4 paid (or possibly paid) but the sent ledger could not be saved (`ledger-unsaved`),
5 possibly paid and recorded: the block was handed to the node and its outcome is unknown (a
timeout, a lost reply, an unexpected error), the attempt IS in the sent ledger, and a retry
with the same ledger is safe (it settles that same block, it never pays twice). Exit 3 also
covers a seller that could not be reached after paying, or any other failure once the record
is saved ("seller_status": null plus "seller_error"; "confirmed": false when it was a bug in
the node client's block_info).

On exit 4 the JSON carries "paid": true when the block was published, or "paid": "unknown"
when the node's reply never arrived so publication itself is uncertain, plus "recorded": false,
"error": "ledger-unsaved", the block_hash, and the `unsaved_record` to add to the ledger by
hand. Do NOT retry before adding it: a retry could pay twice. The seller is not called.
Any other error during the send saves the signed block's record first; then SystemExit, or a
KeyboardInterrupt your own node client raises itself, is raised unchanged, and any other
exception is exit 5. A Ctrl-C pressed at the keyboard is not raised there: Ctrl-C is held for
the whole send (every node call in it, up to the node client's timeouts) and until the ledger
is written, then delivered, never dropped (the JSON says "error": "interrupted", as it does
for a Ctrl-C anywhere after the save until the seller answers: the lock release, the
confirmation wait, signing the payment header, the seller call; when the send itself ended in
an error, "interrupted_error" names it). After printing that JSON the CLI ends itself by
SIGINT, as an uncaught Ctrl-C does, so a shell loop over pay402 stops (shell status 130; where
SIGINT cannot be delivered, e.g. Windows, it exits 5, or 4/1 when the send's own error was
ledger-unsaved/a refusal). If that save fails as well, exit 4 with "paid":
"unknown" is reported instead of the error. Nothing is saved, and no exit 4 is possible, when
this run signed or settled nothing (a replay of a payment already recorded as settled, or a
refusal before signing). Library callers: pay() raises PaidUnrecorded for exit 4; it is NOT
a Refused subclass, so `except Refused` does not catch it. It raises PaidUnknown for exit 5,
also not a Refused subclass. A Ctrl-C after the record is saved is NOT turned into an
Exception by pay(): it raises PaidInterrupted, a KeyboardInterrupt subclass whose `payment`
attribute holds block_hash, ledger, from, to, amount_xno and receipt, so it stops a batch
loop like any Ctrl-C; a Ctrl-C held during a send that then raised PaidUnknown, PaidUnrecorded
or Refused is raised as PaidInterrupted too, chained to that error. Only main() prints it as
JSON "interrupted". Otherwise, for exit 5, a retry that reaches the send and still cannot settle the block
exits 5 again (a refusal before the send, e.g. the price is now over --max-xno, is exit 1:
this run signed nothing, but the earlier block may still have landed).
"""
import base64
import contextlib
import copy
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import sys
import tempfile
import threading
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
    def __init__(self, reason, message, extra=None):
        super().__init__(message)
        self.reason, self.message, self.extra = reason, message, dict(extra or {})


class PaidUnrecorded(Exception):
    """A payment was (or may have been) published but the sent ledger could not be saved.
    Not a refusal, so deliberately NOT a subclass of Refused: a caller that catches Refused
    must not read this as "nothing was paid". The money moved, or may have; `paid` is True
    or "unknown". It carries the same reason/message/extra attributes as Refused."""

    def __init__(self, paid, message, extra=None):
        super().__init__(message)
        self.reason, self.message, self.extra = "ledger-unsaved", message, dict(extra or {})
        self.paid = paid


class PaidUnknown(Exception):
    """The block was signed and handed to the node, its outcome is unknown, and the attempt IS
    saved in the sent ledger (exit 5). Not a refusal: the money may have moved. A retry with
    the same ledger is safe: it settles that same block (one hash, so at most one payment)."""

    def __init__(self, reason, message, extra=None):
        super().__init__(message)
        self.reason, self.message, self.extra = reason, message, dict(extra or {})
        self.paid = "unknown"


class PaidInterrupted(KeyboardInterrupt):
    """Ctrl-C landed after this payment's block was handed to the node and its record was saved.
    A KeyboardInterrupt subclass, NOT an Exception: from Python a Ctrl-C keeps stopping the
    caller (a batch loop catching `except (Refused, PaidUnknown, PaidUnrecorded)` or even
    `except Exception` does not swallow it and pay the next URL). It carries the payment:
    `payment` (the same dict as `extra`: block_hash, ledger, from, to, amount_xno, receipt),
    `paid` ("unknown"), `reason` ("interrupted") and `message`. Only the CLI main() turns it
    into exit 5 JSON. A retry with the same ledger is safe: it reuses that same block.

    It is also raised for a Ctrl-C held during the send when the send itself then ends in an
    error (PaidUnknown, PaidUnrecorded, Refused, anything): the Ctrl-C is not dropped. Then it
    is chained (`__cause__`) to that error, `extra` carries that error's details plus
    `interrupted_error` (its reason), and `paid`/`recorded` are that error's (`recorded` is
    False, with `unsaved_record`, for a PaidUnrecorded; `paid` is False and `recorded` None for
    a Refused)."""

    def __init__(self, message, extra=None, paid="unknown", recorded=True):
        super().__init__(message)
        self.reason, self.message, self.extra = "interrupted", message, dict(extra or {})
        self.payment = self.extra
        self.paid = paid
        self.recorded = recorded


def _interrupted_during(error):
    """A Ctrl-C held during a send that then raised `error`: a PaidInterrupted carrying what
    `error` knows, so the caller stops instead of handling `error` and going on."""
    reason = getattr(error, "reason", None) or type(error).__name__
    message = getattr(error, "message", None) or str(error) or type(error).__name__
    extra = dict(getattr(error, "extra", None) or {}, interrupted_error=reason)
    if isinstance(error, Refused):
        paid, recorded = False, None
    elif isinstance(error, PaidUnrecorded):
        paid, recorded = error.paid, False
    else:
        paid, recorded = getattr(error, "paid", "unknown"), True
    return PaidInterrupted("interrupted (Ctrl-C) during the send, which then ended in %s: %s"
                           % (reason, message), extra, paid=paid, recorded=recorded)


# Refusals that leave this payment's unsettled record in the ledger yet say for certain what
# happened: send_did_not_land (a confirmed block took the slot, nothing was paid),
# idempotency_conflict (the record is for other terms; nothing of this call was signed) and
# duplicate_send (the recorded block is already settled as another entry's one payment, so
# it paid nothing for this URL). Each is final: a retry gets the same answer, so it exits 1.
_SETTLED_REFUSALS = ("send_did_not_land", "idempotency_conflict", "duplicate_send")


class _SigintDeferred:
    """Hold Ctrl-C (SIGINT) while the sent ledger is written, then deliver it: a KeyboardInterrupt
    that lands inside the save would delete the temp file and leave a published block
    unrecorded. Main thread only (signal handlers cannot be set elsewhere); the previous
    handler is always restored. A previous handler of None (one not installed from Python) is
    restored as signal.default_int_handler; a held Ctrl-C under SIG_DFL is re-delivered as a
    real SIGINT once SIG_DFL is back, so it still ends the process.

    A held Ctrl-C is never dropped. If the block is raising an Exception (PaidUnknown,
    PaidUnrecorded, Refused, anything) when it ends, the Ctrl-C is raised as PaidInterrupted
    chained to that exception and carrying its details (see _interrupted_during), so a caller
    that handles that exception still stops; inside another _SigintDeferred it is handed to
    the outer one, which does the same when it ends. Under SIG_IGN it is ignored as usual."""

    def _hold(self, signum, frame):
        self.pending.append(signum)

    def __enter__(self):
        self.pending, self.previous, self.installed = [], None, False
        if threading.current_thread() is threading.main_thread():
            try:
                self.previous = signal.signal(signal.SIGINT, self._hold)
                self.installed = True
            except (ValueError, OSError):
                self.previous = None
        if self.previous is None:
            self.previous = signal.default_int_handler
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.installed:
            return False
        signal.signal(signal.SIGINT, self.previous)
        if not self.pending or self.previous is signal.SIG_IGN:
            return False
        outer = isinstance(getattr(self.previous, "__self__", None), _SigintDeferred)
        if exc_type is None or outer:
            # Ends normally, or nested: deliver to the previous handler (an outer
            # _SigintDeferred only holds it, and converts it when it ends).
            if self.previous is signal.SIG_DFL:
                os.kill(os.getpid(), signal.SIGINT)   # SIG_DFL is back: ends the process
            elif callable(self.previous):
                self.previous(signal.SIGINT, None)    # the default handler raises KeyboardInterrupt
            return False
        if not issubclass(exc_type, Exception):
            return False        # already a KeyboardInterrupt / SystemExit: it stops the caller
        if self.previous is not signal.SIG_DFL and callable(self.previous):
            try:
                self.previous(signal.SIGINT, None)
            except BaseException:
                pass            # the handler stops the caller: say so below, with the report
            else:
                return False    # a handler that does not raise chose to carry on
        raise _interrupted_during(exc) from exc


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
    extra = entry.get("extra")
    if extra and not isinstance(extra, dict):
        # payment_header() reads the binding out of `extra` only after the block is published:
        # an `extra` that is not an object is refused here, before anything is signed. An
        # empty one (null, "", {}) is read as "no extra", as before.
        raise Refused("bad_extra", "the nano entry's extra %r is not a JSON object: not paying"
                      % (extra,))
    binding = (extra or {}).get("binding")
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
        nonce = binding.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            # The nonce is signed into the payment header only after the block is published:
            # a missing or non-string one is refused here, before anything is signed.
            raise Refused("binding_nonce_invalid",
                          "the seller's binding nonce %r is not a non-empty string: not paying"
                          % (nonce,))
        # Every field the payment header signs must encode as UTF-8 (a lone surrogate such as
        # "\ud800" does not), and the binding message itself is built here with a placeholder
        # block hash: a failure there after the send would leave the seller paid and uncalled.
        try:
            nonce.encode("utf-8")
        except UnicodeEncodeError:
            raise Refused("binding_nonce_invalid",
                          "the seller's binding nonce %r is not valid UTF-8 text: not paying"
                          % (nonce,)) from None
        try:
            binding["resource"].encode("utf-8")
        except UnicodeEncodeError:
            raise Refused("binding_resource_mismatch",
                          "the binding resource %r is not valid UTF-8 text, so it cannot be "
                          "signed: not paying" % (binding["resource"],)) from None
        try:
            binding_message("00" * 32, nonce, binding["resource"], entry["payTo"])
        except Exception as exc:
            raise Refused("binding_nonce_invalid",
                          "cannot build the payment header for this binding (%s: %s): not paying"
                          % (type(exc).__name__, exc)) from None
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
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise Refused(what + "-unreadable", "%s is not a regular file (a FIFO, device, "
                              "socket or directory?): not paying" % path)
            if info.st_nlink > 1:
                # The save replaces the file (os.replace), which breaks a hard link: the other
                # name keeps the old content, so a ledger used through both names could pay
                # twice and a pin changed through the other name would go unnoticed.
                name = "sent ledger" if what == "ledger" else "pin file"
                raise Refused(what + "-unreadable", "%s %s has %d hard links; a save would "
                              "replace it and leave the other names stale; copy it to a file "
                              "of its own (cp %s NEW), point pay402 at the copy and stop "
                              "using the other names: not paying"
                              % (name, path, info.st_nlink, shlex.quote(path)))
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


def _ledger_directory(path):
    """The directory the save writes into, read from the path exactly as given (never
    normalised: abspath would resolve 'missing/..' textually where the OS does not)."""
    return os.path.dirname(path) or os.curdir


def _ledger_directory_check(path):
    """Refuse before signing when the sent ledger could not be saved: a payment published and
    then not recorded could make a retry sign a NEW block and pay twice. The check, the load and
    the save all use this one path as given."""
    if path.endswith(os.sep) or (os.altsep and path.endswith(os.altsep)):
        raise Refused("ledger-unwritable", "sent ledger %s ends in a path separator, so it names "
                      "a directory, not a file, and a payment could not be recorded; give the "
                      "ledger's file path; do NOT start a new empty ledger elsewhere if payments "
                      "were already recorded: not paying" % path)
    directory = _ledger_directory(path)
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
    if os.path.islink(path):
        # A symlinked ledger is saved in place of the file it points at (see _ledger_save), so
        # that file's directory must be writable too.
        real_directory = os.path.dirname(os.path.realpath(path))
        if os.path.isdir(real_directory) and not os.access(real_directory, os.W_OK | os.X_OK):
            raise Refused("ledger-unwritable", "sent ledger %s is a symlink to %s, whose "
                          "directory %s is not writable, so a payment could not be recorded; "
                          "fix its permissions: not paying"
                          % (path, os.path.realpath(path), real_directory))


def _lock_path(path):
    """The lock file for `path`: next to the file it really names, so a symlink to it, a path
    through a symlinked directory and the real path all take the same lock. A directory that
    does not exist as typed (e.g. 'missing/..') is left as typed, never resolved textually.
    A dangling link is locked beside the link (its load refuses it), not at its target."""
    if os.path.exists(path):
        return os.path.realpath(path) + ".lock"
    directory = os.path.dirname(path) or os.curdir
    if os.path.isdir(directory):
        return os.path.join(os.path.realpath(directory), os.path.basename(path)) + ".lock"
    return path + ".lock"


@contextlib.contextmanager
def _file_lock(path, reason="pin-file-unwritable"):
    """Exclusive flock on the lock file for path (_lock_path: the real file's path + '.lock';
    POSIX), waiting until it is free. Without fcntl it is a no-op. A lock file that cannot be
    opened or locked is refused as `reason`, before anything is signed."""
    if fcntl is None:
        yield
        return
    lock = _lock_path(path)
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise Refused(reason, "cannot open lock file %s: %s: not paying" % (lock, exc)) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        # e.g. ENOLCK on a filesystem without flock: refuse instead of a bare traceback.
        os.close(fd)
        raise Refused(reason, "cannot lock %s: %s: not paying" % (lock, exc)) from None
    try:
        yield
    finally:
        os.close(fd)


def _ledger_save(path, sent):
    """Atomic replace through a unique temp file in the same directory (mkstemp: O_EXCL, 0600,
    so a planted symlink at a predictable name is never followed). A path that is a symlink
    (a ledger linked to its real file) is saved in place of the file it points at, keeping the
    link: os.replace on the link itself would turn it into a plain file and leave the real
    ledger without the record. The pin file refuses links before it gets here."""
    if os.path.islink(path):
        path = os.path.realpath(path)
    directory = _ledger_directory(path)
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


_ENTRY_ADVICE = ("restore it from a backup or fix it by hand; do NOT delete it - it records "
                 "blocks already sent")


def _ledger_entry_check(path, key, record):
    """The entry already held under this payment's key, if any, must have the shape
    payments.send writes; a wrong one is refused before signing instead of crashing in it."""
    if record is None:
        return
    problems = []
    if not (isinstance(record.get("to"), str) and record["to"]):
        problems.append("'to' is not an address string")
    amount = record.get("amount_raw")
    if not (isinstance(amount, int) and not isinstance(amount, bool) and amount > 0):
        problems.append("'amount_raw' is not a positive integer")
    result = record.get("result")
    if result is not None and not (isinstance(result, dict)
                                   and isinstance(result.get("block_hash"), str)
                                   and result["block_hash"]):
        problems.append("'result' is neither null nor an object with a 'block_hash' string")
    for name, kind, label in (("block_hash", str, "a string"), ("previous", str, "a string"),
                              ("block", dict, "an object")):
        if record.get(name) is not None and not isinstance(record[name], kind):
            problems.append("%r is not %s" % (name, label))
    if problems:
        raise Refused("ledger-malformed", "sent ledger %s: the entry for this payment (%r) is not "
                      "one pay402 wrote (%s): %s: not paying"
                      % (path, key, "; ".join(problems), _ENTRY_ADVICE))


def _ledger_save_after_send(path, sent, key, block_hash, tool_error=None):
    """Save the ledger once payments.send has run. A save that fails here may leave a published
    payment unrecorded, so say so plainly, with the record to add by hand: a retry could pay
    twice. Ctrl-C is held until the save finishes; any exception from the save itself (an
    OSError, or a KeyboardInterrupt raised inside it) becomes PaidUnrecorded, never escapes."""
    with _SigintDeferred():     # a held Ctrl-C is delivered after the save, outside the try
        try:
            _ledger_save(path, sent)
        except BaseException as exc:
            record = sent.get(key)
            if tool_error is not None:
                reason = getattr(tool_error, "reason", type(tool_error).__name__)
                message = getattr(tool_error, "message", None) or str(tool_error) or "interrupted"
            if tool_error is not None and record is None:
                raise Refused(reason, "%s (and the sent ledger %s could not be saved: %s)"
                              % (message, path, exc)) from None
            block_hash = block_hash or (record or {}).get("block_hash")
            unsaved = {key: record}
            if tool_error is None:
                paid = True
                state = "the payment WAS published as block %s" % block_hash
            else:
                paid = "unknown"
                state = ("the payment MAY have been published as block %s (%s: %s)"
                         % (block_hash, reason, message))
            raise PaidUnrecorded(paid, "%s, but the sent ledger %s could not be saved (%s). "
                                 "do NOT retry: a retry could pay twice; add this record to the "
                                 "ledger by hand (merge the unsaved_record object into %s as one "
                                 "more key), then check the block on a block explorer: %s"
                                 % (state, path, str(exc) or type(exc).__name__, path,
                                    json.dumps(unsaved, sort_keys=True)),
                                 {"block_hash": block_hash, "unsaved_record": unsaved,
                                  "ledger": path}) from (tool_error or exc)


def idempotency_key(url, pay_to, raw):
    return "pay402-" + hashlib.sha256(("%s|%s|%d" % (url, pay_to, raw)).encode()).hexdigest()[:40]


def wait_confirmed(node, block_hash, seconds=30, sleep=time.sleep):
    """True once the node calls the block confirmed, False when it cannot say in time. Runs
    after the payment is published and recorded, so no node error may escape it: NodeError, a
    reply cut off mid-read (nanonode.ReplyCutOff, i.e. http.client.HTTPException), ValueError,
    OSError and a reply that is not an object all read as "cannot say yet". A node without
    block_info (NotImplementedError) cannot say, so it is False at once rather than polled. Anything else a custom node client
    raises (a TypeError, say) is a bug in that client and propagates out of here; pay() then
    reports the payment (made and recorded) with "seller_status": null rather than raising."""
    deadline = time.time() + seconds
    while True:
        try:
            info = node.block_info(block_hash)
            if isinstance(info, dict) and info.get("confirmed") is True:
                return True
        except NotImplementedError:
            return False
        except (nanonode.NodeError, nanonode.ReplyCutOff, ValueError, OSError):
            # A node error, a reply cut off mid-read, an unparsable reply, a broken connection:
            # the confirmation is unknown, not the payment. A reply that is not an object
            # (checked above) reads the same. Any other exception (a TypeError in a custom node
            # client, say) is a bug in that client and is raised.
            pass
        if time.time() >= deadline:
            return False
        sleep(1)


def _interrupted_after_send(result, source, summary, ledger, seller=False):
    """Ctrl-C once this payment's block was published and its record saved (a Ctrl-C held during
    the send and delivered after the save, during the confirmation wait, or during the seller
    call, or anywhere between the save and the seller's reply): a PaidInterrupted, which is a
    KeyboardInterrupt carrying the payment. The CLI main() alone turns it into exit 5 JSON,
    never a bare traceback that exits 1 for a payment that went out."""
    block_hash = result["block_hash"]
    when = ("during the seller call (the seller may or may not have received the payment "
            "header)" if seller else "before the seller was called")
    return PaidInterrupted("interrupted (Ctrl-C) after block %s was handed to the node "
                       "and recorded in the sent ledger %s, %s. A retry with the same ledger is "
                       "safe: it reuses that same block (one hash, so at most one payment) and "
                       "then calls the seller" % (block_hash, ledger, when),
                       {"block_hash": block_hash, "ledger": ledger, "from": source,
                        "to": summary["pay_to"], "amount_xno": summary["amount_xno"],
                        "receipt": "https://nanolooker.com/block/%s" % block_hash})


def pay(url, entry, summary, key_file, max_xno, node, method="GET", data=None,
        ledger=None, fetcher=fetch, sleep=time.sleep, confirm_seconds=30):
    keys = _keystore.KeyStore()
    try:
        source = keys.load_file(key_file)
    except _keystore.KeyStoreError as exc:
        raise Refused(exc.reason, exc.message) from None
    ledger = ledger or os.path.expanduser(key_file) + ".pay402-sent.json"
    _ledger_directory_check(ledger)
    # One payment's load -> send -> save runs under an exclusive lock on LEDGER.lock: a second
    # run on the same ledger waits here, then loads the ledger only once the first run's record
    # is on disk, so it settles that record instead of signing a second block, and its save
    # cannot overwrite the first run's with a stale copy.
    # From the moment _send_recorded returns (the record is on disk) until the seller has
    # answered, a Ctrl-C anywhere - the lock release, the confirmation wait, signing the
    # payment header, the seller call - is raised as PaidInterrupted (a KeyboardInterrupt
    # carrying the payment), never a bare KeyboardInterrupt and never an Exception.
    # Likewise any Exception raised once the send is recorded (the lock release, a custom
    # node client's bug while confirming, building the payment header, the seller call, reading
    # its reply) is reported as the payment with "seller_status": null, never raised: the
    # money moved, so it must never look like exit 1 / "nothing signed".
    result = paid = None
    seller = False
    sent_box = {}       # _send_recorded puts the record here while Ctrl-C is still held
    try:
        with _file_lock(ledger, "ledger-unwritable"):
            result = _send_recorded(url, summary, source, keys, max_xno, node, ledger, sent_box)
        paid = _paid_report(result, source, summary)
        block_hash = paid["block_hash"]
        paid["confirmed"] = wait_confirmed(node, block_hash, confirm_seconds, sleep)
        header = payment_header(block_hash, entry, keys.get(source))
        seller = True
        status, _, body = fetcher(url, method=method, data=data,
                                  headers={"X-PAYMENT": header,
                                           "Content-Type": "application/json"}
                                  if data else {"X-PAYMENT": header})
        return dict(paid, seller_status=status,
                    seller_body=body.decode("utf-8", "replace")[:4000])
    except PaidInterrupted:
        raise
    except KeyboardInterrupt:
        if result is None:
            result = sent_box.get("result")
        if result is None:
            raise       # before the record was saved: _send_recorded's own contract
        raise _interrupted_after_send(result, source, summary, ledger, seller=seller) from None
    except Exception as exc:
        if result is None:
            result = sent_box.get("result")
        if result is None:
            raise       # before the record was saved: _send_recorded's own contract
        # The payment is made and recorded; only what came after it failed. Never a refusal.
        reason = getattr(exc, "reason", None)
        if not isinstance(reason, str) or not reason:
            reason = type(exc).__name__
        message = getattr(exc, "message", None)
        if not isinstance(message, str) or not message:
            message = str(exc) or type(exc).__name__
        if not seller:
            message = "the seller was not called (%s: %s)" % (type(exc).__name__, message)
        if paid is None:
            paid = _paid_report(result, source, summary)
        return dict(paid, seller_status=None, seller_error=reason, seller_body=message)


def _paid_report(result, source, summary):
    block_hash = result["block_hash"]
    return {"paid": True, "block_hash": block_hash, "from": source, "to": summary["pay_to"],
            "amount_xno": summary["amount_xno"], "confirmed": False,
            "replayed_send": bool(result.get("replayed")),
            "receipt": "https://nanolooker.com/block/%s" % block_hash}


def _settled_refusal(exc, ledger, key, sent):
    """The pay402 wording for a refusal that leaves this payment's record in the ledger and says
    for certain that nothing was paid for this URL. payments.send advises a NEW idempotency key,
    which pay402 cannot choose (the key is a hash of url, payTo and amount): every run of this
    URL would be refused the same way while the record stays, so name the one way out."""
    record = sent.get(key)
    block_hash = (record or {}).get("block_hash") or "(not recorded)"
    if exc.reason == "duplicate_send":
        return _duplicate_refusal(exc, ledger, key, sent)
    if exc.reason == "send_did_not_land":
        what = ("block %s, recorded for this URL, can never land: a confirmed block took the "
                "slot it was built on" % block_hash)
    else:
        return Refused(exc.reason, exc.message)
    return Refused(exc.reason, "%s, so nothing was paid for this URL by it. pay402 derives the "
                   "idempotency key from (url, payTo, amount) and cannot switch to a new one, so "
                   "every run for this URL is refused like this while entry %r stays in the sent "
                   "ledger %s. To pay this URL, remove that one entry (only %r) from the ledger "
                   "by hand and run again: that signs a new block. Not paying"
                   % (what, key, ledger, key))


def _duplicate_refusal(exc, ledger, key, sent):
    """duplicate_send: this URL's block is the identical block another ledger entry holds.
    payments.send raises it whether or not that holder has settled, so the advice depends on
    which: an unsettled holder is retried first (removing anything then could pay twice)."""
    record = sent.get(key)
    # The block: this URL's recorded one, or (nothing recorded yet) the one payments.send names.
    block_hash = (record or {}).get("block_hash")
    if not block_hash:
        found = re.search(r"block ([0-9A-Fa-f]{64})", exc.message or "")
        block_hash = found.group(1) if found else None
    holder = None
    for other, other_record in sent.items():
        if (other == key or not isinstance(other_record, dict) or not block_hash
                or str(other_record.get("block_hash") or "").upper() != block_hash.upper()):
            continue
        # Of several holders, an unsettled one decides the advice: retry it first.
        if holder is None or other_record.get("result") is None:
            holder = other
    holder_record = sent.get(holder) or {}
    block_hash = block_hash or "(unknown)"
    same = ("(same account, frontier, seller and amount sign to the same block, and one block "
            "is one payment)")
    if holder is None or holder_record.get("result") is None:
        who = repr(holder) if holder is not None else "(another entry)"
        return Refused(exc.reason, "this URL's payment would be block %s, the identical block "
                       "ledger entry %s holds and has not settled yet %s, so nothing was paid "
                       "for this URL by this run. Retry first the URL whose payment is recorded "
                       "under %s (run pay402 for it with this same sent ledger %s): that "
                       "settles whether that block landed. Do NOT remove any ledger entry "
                       "while it is not settled: that could pay twice. Then run this URL again. "
                       "Not paying" % (block_hash, who, same, who, ledger))
    if record is None:
        return Refused(exc.reason, "this URL's payment would be block %s, the identical block "
                       "ledger entry %r already settled as its own payment %s, so nothing was "
                       "paid for this URL by this run and nothing was recorded for it. Run it "
                       "again once that block is confirmed (the account's frontier moves past "
                       "it and a new block is signed). Not paying" % (block_hash, holder, same))
    return Refused(exc.reason, "block %s, recorded for this URL, is the identical block ledger "
                   "entry %r already settled as its own payment %s, so nothing was paid for "
                   "this URL by it. pay402 derives the idempotency key from (url, payTo, "
                   "amount) and cannot switch to a new one, so every run for this URL is "
                   "refused like this while entry %r stays in the sent ledger %s. To pay this "
                   "URL, remove that one entry (only %r) from the ledger by hand and run "
                   "again: that signs a new block. Not paying"
                   % (block_hash, holder, same, key, ledger, key))


def _send_recorded(url, summary, source, keys, max_xno, node, ledger, sent_box=None):
    """Load the sent ledger, send (or settle) this payment's block and save the ledger. The
    caller holds the ledger lock for all of it. When `sent_box` (a dict) is given, the result is
    also put in it under "result" while Ctrl-C is still held, so a Ctrl-C landing after this
    returns but before the caller has assigned the return value still finds the payment."""
    sent = _ledger_load(ledger)
    env = {"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_MAX_SEND_XNO": max_xno}
    key = idempotency_key(url, summary["pay_to"], int(summary["amount_raw"]))
    _ledger_entry_check(ledger, key, sent.get(key))
    # Saved after the send only when THIS run changed the ledger (it signed a block, or
    # settled/freed an earlier one). A replay of an already-settled record, or a refusal
    # before anything was signed, has nothing new to record: a failed save there must not
    # be reported as a payment.
    before = copy.deepcopy(sent)

    def unknown(reason, message):
        # The attempt is on disk (saved just before this is called): say "may have paid",
        # never "refused before paying".
        record = sent[key]
        return PaidUnknown(reason, "%s; the payment MAY have been published as block %s and is "
                           "recorded in the sent ledger %s. A retry with the same ledger is "
                           "safe: it settles that same block instead of paying again"
                           % (message, record.get("block_hash"), ledger),
                           {"block_hash": record.get("block_hash"), "ledger": ledger})

    # Ctrl-C is held for the WHOLE send (building the block and every node call it makes, which
    # can last as long as the node client's timeouts) and whichever save follows it, so one
    # landing between the send returning (or raising) and the save starting cannot leave a
    # signed block unsaved. A held Ctrl-C is delivered once the record is on disk (here,
    # before the seller call).
    saved = None
    try:
        with _SigintDeferred():
            saved = _send_and_save(source, summary, keys, node, sent, env, key, before, ledger,
                                   unknown)
            if sent_box is not None:
                sent_box["result"] = saved
    except KeyboardInterrupt:
        if saved is None:
            raise
        # A Ctrl-C held during a send that succeeded, delivered once its record was saved.
        raise _interrupted_after_send(saved, source, summary, ledger) from None
    return saved


def _send_and_save(source, summary, keys, node, sent, env, key, before, ledger, unknown):
    """payments.send for this payment, then the ledger save (see _send_recorded)."""
    try:
        result = payments.send(source, summary["pay_to"], summary["amount_xno"], key, node, keys,
                               sent, env=env)
        block_hash = result["block_hash"]
        if sent != before:
            _ledger_save_after_send(ledger, sent, key, block_hash)
    except PaidUnrecorded:
        raise
    except payments.ToolError as exc:
        if sent != before:
            _ledger_save_after_send(ledger, sent, key, None, exc)
        if ((sent.get(key) or {"result": True}).get("result") is None
                and exc.reason not in _SETTLED_REFUSALS):
            # A block for this payment was handed to the node and no reply has settled it:
            # either THIS run just did that (a timeout, a lost reply), or an earlier run did
            # and this retry could not settle it either (send_outcome_unknown, ledger
            # unchanged). Either way the money may have moved: never paid false.
            raise unknown(exc.reason, exc.message) from None
        raise _settled_refusal(exc, ledger, key, sent) from None
    except BaseException as exc:
        # Any other error (a bug, a node client raising something unexpected, Ctrl-C /
        # KeyboardInterrupt, SystemExit) may come after the block was handed to the node:
        # payments.send records the attempt before the broadcast. Save that record so a
        # retry settles it instead of paying again. Ctrl-C and SystemExit are then re-raised
        # unchanged; any other exception becomes PaidUnknown (exit 5) so the CLI prints JSON.
        # If that save fails too, PaidUnrecorded (exit 4, paid "unknown") is raised in its
        # place, chained to the original error.
        if sent != before and sent.get(key) is not None:
            _ledger_save_after_send(ledger, sent, key, None, exc)
            if isinstance(exc, Exception):
                raise unknown(type(exc).__name__, "%s: %s" % (type(exc).__name__, exc)) from exc
        raise
    return result


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


def _die_by_sigint(fallback):
    """End the process by SIGINT once a Ctrl-C has been reported: a shell (a bash `for` loop)
    stops only when its child died by SIGINT, not when it exited with some code. Output is
    flushed first. Where SIGINT cannot be delivered to this process (not POSIX, e.g. Windows,
    or not the main thread, or SIGINT blocked), `fallback` is returned as the exit code."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    if os.name == "posix" and threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGINT)
        except (AttributeError, OSError, ValueError):
            pass
    return fallback


def _stdout_to_devnull():
    """Point stdout at os.devnull once it cannot take output, so nothing later (a print, the
    interpreter's own flush at exit) fails on it again (Python's documented recipe for
    SIGPIPE). Never raises."""
    try:
        if sys.stdout is None:              # fd 1 was closed at start (`pay402 ... >&-`)
            sys.stdout = open(os.devnull, "w")
            return
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            if devnull != fd:
                os.close(devnull)
    except Exception:       # noqa: BLE001 - not a real file (a StringIO, say): nothing to do
        pass


def _report(out, obj):
    """Print the report of a run that signed a block and flush it now, so a stdout that cannot
    take it shows up here. Never raises an Exception: if stdout cannot take it (None because
    fd 1 was closed at start, `>&-`; BrokenPipeError when the reader is gone, `| head -0`;
    ENOSPC on `> /dev/full`; any other error while writing or flushing) the report is lost,
    stdout is pointed at os.devnull (_stdout_to_devnull) and the run goes on to its exit code,
    unchanged. Returns True only when the write and the flush both returned."""
    try:
        if sys.stdout is None:
            _stdout_to_devnull()
            return False
        out(obj)
        sys.stdout.flush()
        return True
    except Exception:       # noqa: BLE001 - see the docstring: the exit code must not change
        _stdout_to_devnull()
        return False


class _PaidReport:
    """The report and exit code of a run that signed a block (exit 0, 3, 4, 5, or a Ctrl-C
    after paying), printed at most once, with Ctrl-C held.

    main() fills it with plain attribute stores only (no call a Ctrl-C could interrupt):
    `outcome` is pay()'s result dict or the PaidUnrecorded / PaidUnknown / PaidInterrupted it
    raised, `summary` the plan, `interrupted` True when a Ctrl-C landed after pay() returned.
    finish() then works out the report and the exit code, prints the report (see _report: a
    stdout that cannot take it loses the report, never the exit code) and returns the code,
    or ends the run by SIGINT if a Ctrl-C landed at any point (see _die_by_sigint), the code
    being the fallback. `printed` is set only from _report's return value (the write and the
    flush returned), never on an exception path, so a Ctrl-C that cuts a step short makes
    finish() redo what is not done yet and never prints the report twice."""

    def __init__(self, out):
        self.out = out
        self.outcome = None
        self.summary = None
        self.interrupted = False
        self.code = None
        self.report = None
        self.printed = False    # the report was written and flushed (True only from _report)
        self.lost = False       # stdout could not take it (or it could not be built): not retried
        self.pending = []
        self.previous = None
        self.holding = False
        self.cannot_hold = False

    def _hold(self, signum, frame):
        self.pending.append(signum)

    def _install(self):
        """Hold Ctrl-C from here on. A Ctrl-C that landed just before is delivered by
        signal.signal() itself (it runs pending handlers first) as a KeyboardInterrupt with
        nothing installed, so finish() just tries again."""
        if self.holding or self.cannot_hold:
            return
        if threading.current_thread() is not threading.main_thread():
            self.cannot_hold = True
            return
        try:
            if signal.getsignal(signal.SIGINT) is signal.SIG_IGN:
                self.cannot_hold = True     # Ctrl-C is ignored here: nothing to hold
                return
            previous = signal.signal(signal.SIGINT, self._hold)
        except (ValueError, OSError):
            self.cannot_hold = True
            return
        self.previous, self.holding = previous, True

    def _restore(self):
        if self.holding:
            previous = self.previous if self.previous is not None else signal.default_int_handler
            signal.signal(signal.SIGINT, previous)
            self.holding = False

    def _build(self):
        """The report and the exit code for `outcome` (see main and the module docstring)."""
        outcome = self.outcome
        if isinstance(outcome, PaidInterrupted):
            self.interrupted = True
            cause = outcome.__cause__
            if isinstance(cause, PaidUnrecorded):
                code = 4
            elif isinstance(cause, Refused):
                code = 2 if cause.reason == "usage" else 1
            else:
                code = 5
            report = _interrupted_report(outcome)
        elif isinstance(outcome, PaidUnrecorded):
            code, report = 4, dict(outcome.extra, paid=outcome.paid, recorded=False,
                                   error=outcome.reason, message=outcome.message)
        elif isinstance(outcome, PaidUnknown):
            code, report = 5, dict(outcome.extra, paid="unknown", recorded=True,
                                   error=outcome.reason, message=outcome.message)
        else:
            code, report = _paid_code(outcome, self.summary), outcome
        self.report, self.code = report, code

    def _fallback_code(self):
        if self.code is not None:
            return self.code
        if isinstance(self.outcome, dict):
            return 3        # paid; whether the seller accepted it could not be read
        if isinstance(self.outcome, PaidUnrecorded):
            return 4
        return 5           # possibly paid and recorded (PaidUnknown / PaidInterrupted)

    def finish(self):
        for _attempt in range(3):       # bounded: a stream of Ctrl-Cs cannot loop forever
            try:
                self._install()
                if self.code is None:
                    try:
                        self._build()
                    except Exception:   # noqa: BLE001 - a bug building it loses the report only
                        self.code = self._fallback_code()
                        self.lost = True
                if not (self.printed or self.lost):
                    written = _report(self.out, self.report)    # never raises an Exception
                    self.printed, self.lost = written, not written
                self._restore()
                break
            except KeyboardInterrupt:
                self.interrupted = True
        if self.holding:                # three Ctrl-Cs raised by code, not by the terminal
            try:
                self._restore()
            except KeyboardInterrupt:
                self.interrupted = True
        if self.interrupted or self.pending:
            return _die_by_sigint(self._fallback_code())
        return self._fallback_code()


def main(argv=None, fetcher=fetch, node=None, sleep=time.sleep):
    args = list(sys.argv[1:] if argv is None else argv)
    out = lambda obj: print(json.dumps(obj, indent=2, sort_keys=True))  # noqa: E731
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    paid = _PaidReport(out)
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
        result = None
        paid.summary = summary
        # The block is signed once pay() returns: from there nothing may end as exit 1 (a
        # traceback). Only plain stores happen here until _PaidReport.finish() holds Ctrl-C.
        try:
            result = pay(url, entry, summary, key_file, max_xno, node, method, body, ledger,
                         fetcher, sleep)
        except KeyboardInterrupt:
            if result is None:
                raise       # inside pay(): PaidInterrupted (below) or nothing signed yet
            paid.interrupted = True     # a Ctrl-C once pay() returned: report, then SIGINT
        paid.outcome = result
    except (PaidUnrecorded, PaidUnknown, PaidInterrupted) as exc:
        # PaidInterrupted: the CLI alone turns a Ctrl-C after the save into JSON; pay() raises
        # it. finish() then ends the run by SIGINT, as an uncaught Ctrl-C would, so a shell
        # loop running it stops too.
        paid.outcome = exc
    except Refused as exc:
        out(dict(exc.extra, paid=False, refused=exc.reason, message=exc.message))
        return 2 if exc.reason == "usage" else 1
    # Every run that signed a block (exit 0, 3, 4, 5, or Ctrl-C after paying) reports here,
    # once, with Ctrl-C held; a stdout that cannot take the report does not change the exit
    # code. A Ctrl-C that escapes finish() (one landing before the hold is installed, or after
    # it is lifted) only makes it run again: what is done (the report printed) is not redone.
    for _attempt in range(3):
        try:
            return paid.finish()
        except KeyboardInterrupt:
            paid.interrupted = True
    return _die_by_sigint(paid._fallback_code())


def _paid_code(result, summary):
    """The exit code of a run whose pay() returned (0: the seller accepted it, else 3); the
    pin read before paying, if any, is added to the report."""
    if "pin" in summary:
        result["pin"] = summary["pin"]
    status = result["seller_status"]
    return 0 if status is not None and 200 <= status < 300 else 3


def _interrupted_report(exc):
    """The CLI JSON for a PaidInterrupted."""
    report = dict(exc.extra, paid=exc.paid, error=exc.reason, message=exc.message)
    if exc.recorded is not None:
        report["recorded"] = exc.recorded
    if isinstance(exc.__cause__, Refused):
        report["refused"] = exc.__cause__.reason
    return report


if __name__ == "__main__":
    sys.exit(main())
