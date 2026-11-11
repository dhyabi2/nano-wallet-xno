"""work_service: PROTOTYPE of a pay-per-work endpoint priced in XNO. Not wired into the wallet, spends nothing.

Why: a send needs 2**29 expected hashes. Pure Python does that in minutes, and no free public node answers
`work_generate`. A caller with no cores to spare (a hosted agent that cannot run a background process) can instead
BUY the work. Work is a function of the account's frontier only, so the seller learns nothing it could abuse: not the
amount, not the destination, not a key, and the buyer verifies the answer itself (`work.validates`) before using it.

Protocol (x402 shape, same as pay402.py):
  POST /work  {"hash": "<64 hex frontier>", "threshold": "send"|"receive"}
    -> 402 with accepts[{scheme:"exact", network:"nano:mainnet", asset:"XNO", maxAmountRequired:<raw>, payTo, resource}]
  retry with header X-PAYMENT (base64 JSON, payload.blockHash = a confirmed send to payTo of >= price)
    -> 200 {"work": "<16 hex>", "difficulty": "..."}  after the payment block verifies; a payment block buys ONE work.

Refusals (all fail closed): no price configured; payTo not set; threshold not send/receive (test thresholds only with
allow_test); payment block not confirmed / wrong destination / below price / already used; work that does not validate.

The payment check is injected (`verify_payment`) so tests and this repo run with no node. The default reads one block via a
public node (read-only). Price is operator policy: `price_raw_per_gigahash` is required, there is NO default.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import work as _work  # noqa: E402  (the wallet's own work.py)

THRESHOLDS = {"send": _work.SEND_THRESHOLD, "receive": _work.RECEIVE_THRESHOLD}


class Refused(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def price_raw(threshold: int, price_raw_per_gigahash: int) -> int:
    hashes = _work.expected_attempts(threshold)
    return max(1, int(hashes * int(price_raw_per_gigahash) / 1e9))


class Ledger:
    """One payment block buys one piece of work, remembered across restarts."""

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS used(block TEXT PRIMARY KEY, root TEXT NOT NULL)")
        self.lock = threading.Lock()

    def claim(self, block, root):
        with self.lock:
            try:
                self.db.execute("INSERT INTO used VALUES(?,?)", (block.upper(), root))
                self.db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def release(self, block):  # work failed after claiming: give the payment back its one use
        with self.lock:
            self.db.execute("DELETE FROM used WHERE block=?", (block.upper(),))
            self.db.commit()


class WorkService:
    def __init__(self, pay_to, price_raw_per_gigahash, verify_payment, solver=None, ledger=None, allow_test=False,
                 resource="/work"):
        if not pay_to:
            raise Refused("no_pay_to", "set the payee account explicitly")
        if not price_raw_per_gigahash or int(price_raw_per_gigahash) <= 0:
            raise Refused("no_price", "price_raw_per_gigahash has no default; the operator sets it")
        self.pay_to, self.price = pay_to, int(price_raw_per_gigahash)
        self.verify, self.ledger, self.allow_test, self.resource = verify_payment, ledger or Ledger(), allow_test, resource
        self.solver = solver or (lambda root, thr: _work.solve_parallel(root, thr))

    def _threshold(self, name):
        if name in THRESHOLDS:
            return THRESHOLDS[name]
        if self.allow_test and isinstance(name, int):
            return name
        raise Refused("bad_threshold", "threshold must be 'send' or 'receive'")

    def quote(self, root_hex, threshold_name):
        root = self._root(root_hex)
        thr = self._threshold(threshold_name)
        amount = price_raw(thr, self.price)
        return {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
                "maxAmountRequired": str(amount), "payTo": self.pay_to, "resource": self.resource,
                "description": "proof-of-work for a Nano block over this root; one payment block buys one work",
                "extra": {"root": root.hex().upper(), "threshold": hex(thr)}}]}

    @staticmethod
    def _root(root_hex):
        try:
            root = bytes.fromhex(root_hex)
        except (ValueError, TypeError):
            raise Refused("bad_root", "hash is not hex")
        if len(root) != 32:
            raise Refused("bad_root", "hash must be 32 bytes")
        return root

    def serve(self, root_hex, threshold_name, x_payment=None):
        """-> (status, body). 402 until a valid, unused payment arrives."""
        try:
            q = self.quote(root_hex, threshold_name)
        except Refused as e:
            return 400, {"error": e.code, "detail": e.detail}
        if not x_payment:
            return 402, q
        try:
            block = json.loads(base64.b64decode(x_payment, validate=True))["payload"]["blockHash"]
        except Exception:
            return 400, {"error": "bad_payment_header"}
        need = int(q["accepts"][0]["maxAmountRequired"])
        ok, why = self.verify(block, self.pay_to, need)
        if not ok:
            return 402, dict(q, error="payment_not_verified", detail=why)
        root = self._root(root_hex)
        if not self.ledger.claim(block, root_hex.upper()):
            return 402, dict(q, error="payment_already_used")
        thr = self._threshold(threshold_name)
        try:
            w = self.solver(root, thr)
            if not _work.validates(root, w, thr):
                raise _work.WorkUnavailable("solver returned work that does not validate")
        except Exception as e:
            self.ledger.release(block)
            return 503, {"error": "work_unavailable", "detail": str(e)[:120], "note": "payment not consumed"}
        return 200, {"work": w, "difficulty": "%016x" % _work.difficulty(root, w), "threshold": hex(thr)}


def get_work(root_hex, threshold_name, fetch, max_price_raw, pay=None):
    """Client side, refusal-only. `fetch(root, threshold, x_payment)` -> (status, body) is the transport.
    Without `pay` it only returns the quote. It never pays above max_price_raw, and it checks the work itself."""
    status, body = fetch(root_hex, threshold_name, None)
    if status == 200:
        pass
    elif status != 402:
        raise Refused("service_error", f"HTTP {status}")
    else:
        entry = body["accepts"][0]
        if entry["network"] != "nano:mainnet":
            raise Refused("wrong_network", entry["network"])
        amount = int(entry["maxAmountRequired"])
        if amount > int(max_price_raw):
            raise Refused("over_cap", f"asked {amount} raw, cap {max_price_raw}")
        if pay is None:
            return {"quote": entry, "paid": False}
        block = pay(entry["payTo"], amount)
        header = base64.b64encode(json.dumps({"x402Version": 1, "scheme": "exact", "network": "nano:mainnet",
                                              "payload": {"blockHash": block}}).encode()).decode()
        status, body = fetch(root_hex, threshold_name, header)
        if status != 200:
            raise Refused("not_delivered", f"HTTP {status} {body.get('error')}")
    root = bytes.fromhex(root_hex)
    thr = int(body["threshold"], 16)
    if thr < THRESHOLDS.get(threshold_name, thr) or not _work.validates(root, body["work"], thr):
        raise Refused("bad_work", "the returned work does not validate; do not use it")
    return {"work": body["work"], "paid": True}


def make_handler(service):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != service.resource:
                return self._send(404, {"error": "not_found"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(min(n, 4096)))
                status, body = service.serve(req.get("hash", ""), req.get("threshold", "send"),
                                             self.headers.get("X-PAYMENT"))
            except (ValueError, AttributeError):
                status, body = 400, {"error": "bad_request"}
            self._send(status, body)

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    return H


def serve_forever(service, host="127.0.0.1", port=8403):
    ThreadingHTTPServer((host, port), make_handler(service)).serve_forever()
