"""nano-pay402: dry-run by default, refuses before it signs, never pays twice.

No test here opens a socket except the redirect law (127.0.0.1 only); the seller is a
function, the node is fakenode.FakeNode, and no real payment is possible.
"""
import base64
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ed25519_blake2b as ed
import fakenode
import keystore
import nanoaddr
import payments
import pay402

RAW = 10 ** 30
SELLER_KEY = bytes.fromhex("33" * 32)
SELLER = nanoaddr.encode(ed.public_key_from_private(SELLER_KEY))
PRICE = 5 * 10 ** 28            # 0.05 XNO
URL = "https://seller.example/api/audit"
NONCE = "7fffffff.00112233445566778899aabbccddeeff"
# Same vector as dual-rail's tests/test_binding.py: both repos must produce this signature.
VECTOR_PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
VECTOR_SIGNATURE = ("4FD7E244EB8A7C4ABB8B1E8AF1A232B02BB696E28729048F89AB825883C479DA"
                    "0B166E755AE8DF2F94F85A3C962E28254629F50553C9D54B633999511FC52E05")


def entry(**kw):
    e = {"scheme": "exact", "network": "nano:mainnet", "asset": "XNO", "payTo": SELLER,
         "amount": str(PRICE), "maxAmountRequired": str(PRICE)}
    e.update(kw)
    return e


def challenge(*entries):
    return {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "base", "asset": "USDC",
                                           "amount": "10000", "payTo": "0xabc"}] + list(entries)}


class Seller:
    """A fetcher: 402 until a payment header arrives, then `answer`."""

    def __init__(self, doc, answer=(200, b'{"ok": true}')):
        self.doc, self.answer, self.requests = doc, list(answer) if isinstance(answer, list) else [answer], []

    def __call__(self, url, method="GET", data=None, headers=None, timeout=20):
        self.requests.append((url, dict(headers or {})))
        if "X-PAYMENT" not in (headers or {}):
            return 402, {}, json.dumps(self.doc).encode()
        status, body = self.answer.pop(0) if len(self.answer) > 1 else self.answer[0]
        return status, {}, body


def run(argv, fetcher, node=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = pay402.main(argv, fetcher=fetcher, node=node, sleep=lambda s: None)
    return code, json.loads(buf.getvalue())


class Funded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.node = fakenode.FakeNode()
        keys = keystore.KeyStore()
        self.key_file = os.path.join(self.tmp.name, "key")
        created = payments.create_address(store="file", path=self.key_file, keys=keys)
        self.payer = created["address"]
        self.node.fund(self.payer, 10 * RAW)
        payments.receive(self.payer, self.node, keys)

    def sends(self):
        return [b for b in self.node.published if b.get("_subtype") == "send" or
                (b["link"] == nanoaddr.decode(SELLER).hex().upper())]


class DryRun(unittest.TestCase):
    def test_dry_run_is_the_default_and_touches_no_key_and_no_node(self):
        seller = Seller(challenge(entry()))
        code, out = run([URL], seller)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual((out["pay_to"], out["amount_raw"], out["amount_xno"]), (SELLER, str(PRICE), "0.05"))
        self.assertEqual(len(seller.requests), 1)
        self.assertNotIn("X-PAYMENT", seller.requests[0][1])

    def test_refusals_before_anything_is_signed(self):
        bad_addr = SELLER[:-1] + ("1" if SELLER[-1] != "1" else "3")
        cases = {
            "no_nano_entry": challenge(),
            "bad_pay_to": challenge(entry(payTo=bad_addr)),
            "bad_amount": challenge(entry(amount="0.05", maxAmountRequired="0.05")),
            "binding_scheme_unsupported": challenge(entry(extra={"binding": {"scheme": "other/9"}})),
            "binding_resource_mismatch": challenge(entry(extra={"binding": {
                "scheme": "nano-block-sig/1", "nonce": NONCE, "resource": "/elsewhere"}})),
        }
        for reason, doc in cases.items():
            code, out = run([URL], Seller(doc))
            self.assertEqual((code, out["refused"]), (1, reason), reason)

    def test_over_cap_and_non_402(self):
        self.assertEqual(run([URL, "--max-xno", "0.01"], Seller(challenge(entry())))[1]["refused"], "over_cap")
        self.assertEqual(run([URL, "--max-xno", "0.05"], Seller(challenge(entry())))[0], 0)
        self.assertEqual(run([URL, "--max-xno", "abc"], Seller(challenge(entry())))[1]["refused"], "bad_cap")
        code, out = run([URL], lambda *a, **k: (200, {}, b"hi"))
        self.assertEqual(out["refused"], "not_a_402")

    def test_paying_requires_a_cap_and_a_key(self):
        seller = Seller(challenge(entry()))
        self.assertEqual(run([URL, "--pay"], seller)[1]["refused"], "cap_required")
        self.assertEqual(run([URL, "--pay", "--max-xno", "1"], seller)[1]["refused"], "key_required")
        self.assertEqual(len(seller.requests), 2, "nothing but the 402 fetches happened")

    def test_v2_payment_required_header_is_read(self):
        header = base64.b64encode(json.dumps(challenge(entry())).encode()).decode()
        code, out = run([URL], lambda *a, **k: (402, {"payment-required": header}, b""))
        self.assertEqual(code, 0)
        self.assertEqual(out["pay_to"], SELLER)

    def test_a_redirect_is_not_followed(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/other")
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        status, _, _ = pay402.fetch("http://127.0.0.1:%d/x" % server.server_port)
        self.assertEqual(status, 302)


class Paying(Funded):
    def pay(self, seller, extra=()):
        return run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1", *extra],
                   seller, self.node)

    def test_pays_exactly_the_quote_and_presents_the_block(self):
        seller = Seller(challenge(entry()))
        code, out = self.pay(seller)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["paid"] and out["confirmed"])
        block = [b for b in self.node.published if _hash(b) == out["block_hash"]][0]
        self.assertEqual(block["link"], nanoaddr.decode(SELLER).hex().upper())
        self.assertEqual(int(self.node.accounts[self.payer]["balance_raw"]), 10 * RAW - PRICE)
        header = seller.requests[-1][1]["X-PAYMENT"]
        doc = json.loads(base64.b64decode(header))
        self.assertEqual(doc["payload"]["blockHash"], out["block_hash"])
        self.assertEqual(doc["network"], "nano:mainnet")
        self.assertNotIn("binding", doc["payload"])
        private = open(self.key_file).read().strip()
        self.assertNotIn(private, json.dumps(out))

    def test_binding_is_signed_by_the_paying_key(self):
        e = entry(extra={"binding": {"scheme": "nano-block-sig/1", "nonce": NONCE, "resource": "/api/audit"}})
        seller = Seller(challenge(e))
        code, out = self.pay(seller)
        self.assertEqual(code, 0, out)
        doc = json.loads(base64.b64decode(seller.requests[-1][1]["X-PAYMENT"]))
        sig = bytes.fromhex(doc["payload"]["binding"]["signature"])
        msg = pay402.binding_message(out["block_hash"], NONCE, "/api/audit", SELLER)
        self.assertTrue(ed.verify(msg, sig, nanoaddr.decode(self.payer)) if hasattr(ed, "verify")
                        else sig == ed.sign(msg, bytes.fromhex(open(self.key_file).read().strip()),
                                            nanoaddr.decode(self.payer)))

    def test_cross_repo_vector(self):
        priv = bytes.fromhex("11" * 32)
        self.assertEqual(pay402.binding_signature(priv, "AB" * 32, NONCE, "/report", VECTOR_PAY_TO),
                         VECTOR_SIGNATURE)

    def test_a_failed_retry_never_pays_twice(self):
        seller = Seller(challenge(entry()), answer=[(500, b"oops")])
        code, out = self.pay(seller)
        self.assertEqual(code, 3)
        first = out["block_hash"]
        seller2 = Seller(challenge(entry()), answer=[(200, b"ok")])
        code, out = self.pay(seller2)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["block_hash"], first)
        self.assertTrue(out["replayed_send"])
        paid = [b for b in self.node.published if b["link"] == nanoaddr.decode(SELLER).hex().upper()]
        self.assertEqual(len(paid), 1)

    def test_the_cap_binds_the_send_itself(self):
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.01"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual(out["refused"], "over_cap")
        self.assertEqual([b for b in self.node.published if b["link"] == nanoaddr.decode(SELLER).hex().upper()], [])

    def test_ledger_file_is_private(self):
        self.pay(Seller(challenge(entry())))
        mode = stat.S_IMODE(os.stat(self.key_file + ".pay402-sent.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_insufficient_balance_is_refused_not_crashed(self):
        e = entry(amount=str(50 * RAW), maxAmountRequired=str(50 * RAW))
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "100"],
                        Seller(challenge(e)), self.node)
        self.assertEqual((code, out["refused"]), (1, "insufficient_balance"))


def _hash(block):
    return fakenode._hash_of(block)


if __name__ == "__main__":
    unittest.main()
