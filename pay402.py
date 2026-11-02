"""nano-pay402 - buy from an x402 seller that has priced XNO. Dry-run by default.

    nano-pay402 https://seller.example/api/audit                      # DRY RUN: show what would be paid
    nano-pay402 URL --max-xno 0.1                                     # dry run, also checks the cap
    nano-pay402 URL --pay --key-file ~/.nano/key --max-xno 0.1        # really pay (needs a funded key file)

What it does, in order, and stops at the first thing that is wrong:

  1. Fetches URL and expects HTTP 402 with an x402 `accepts[]` (body JSON, or the base64
     `payment-required` header). Redirects are NOT followed: a 402 from another host than the
     one you named is not a price you agreed to.
  2. Picks the `nano:mainnet` / `nano-mainnet` entry. Refuses if there is none.
  3. Checks `payTo` against its checksum and the price as an integer count of raw (no floats).
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
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, method="GET", data=None, headers=None, timeout=20):
    """(status, headers dict lower-cased, body bytes). Never follows a redirect."""
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=dict({"User-Agent": USER_AGENT}, **(headers or {})))
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, \
                response.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(MAX_BODY)
    except (urllib.error.URLError, OSError) as exc:
        raise Refused("unreachable", "could not reach %s: %s" % (url, getattr(exc, "reason", exc))) from None


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
        wanted = resource_key(urllib.parse.unquote(urllib.parse.urlsplit(url).path))
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

def _ledger_load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ledger_save(path, sent):
    temp = path + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(sent, handle)
    os.replace(temp, path)


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
        if len(args) != 1 or args[0].startswith("--"):
            raise Refused("usage", "expected exactly one URL, got %r" % args)
        url = args[0]
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise Refused("usage", "URL must be http or https")
        body = data.encode("utf-8") if data is not None else None
        status, headers, response = fetcher(url, method=method, data=body,
                                            headers={"Content-Type": "application/json"} if body else {})
        accepts = read_accepts(status, headers, response)
        summary, entry = plan(url, accepts, max_xno)
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
        out(result)
        return 0 if 200 <= result["seller_status"] < 300 else 3
    except Refused as exc:
        out({"paid": False, "refused": exc.reason, "message": exc.message})
        return 2 if exc.reason == "usage" else 1


if __name__ == "__main__":
    sys.exit(main())
