"""work_service prototype. No node, no funds, no network beyond 127.0.0.1; the solver is the real one at a TEST threshold."""
import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import work as W  # noqa: E402
import work_service as ws  # noqa: E402

ROOT = "BCE621224274F7DCB1B9BFB212CF9E5CADC50B47E8BEA5C2E4ED0F063566D85B"
PAYTO = "nano_1" + "a" * 59
TEST_THR = 0xFFFF000000000000  # ~65k hashes: instant, and test-only (allow_test)
BLOCK = "AB" * 32


def header(block=BLOCK):
    return base64.b64encode(json.dumps({"payload": {"blockHash": block}}).encode()).decode()


def verifier(accept=True, why="ok"):
    calls = []
    def v(block, pay_to, need):
        calls.append((block, pay_to, need))
        return accept, why
    v.calls = calls
    return v


def service(**kw):
    kw.setdefault("verify_payment", verifier())
    kw.setdefault("allow_test", True)
    return ws.WorkService(PAYTO, 10 ** 24, **kw)


class Policy(unittest.TestCase):
    def test_no_price_no_default(self):
        with self.assertRaises(ws.Refused) as c:
            ws.WorkService(PAYTO, 0, verifier())
        self.assertEqual(c.exception.code, "no_price")

    def test_no_payto(self):
        with self.assertRaises(ws.Refused):
            ws.WorkService("", 1, verifier())

    def test_price_scales_with_threshold(self):
        self.assertEqual(ws.price_raw(W.SEND_THRESHOLD, 10 ** 24) // ws.price_raw(W.RECEIVE_THRESHOLD, 10 ** 24), 64)

    def test_test_threshold_refused_unless_allowed(self):
        s = ws.WorkService(PAYTO, 1, verifier())
        self.assertEqual(s.serve(ROOT, TEST_THR)[0], 400)
        self.assertEqual(s.serve(ROOT, "easy")[0], 400)


class Flow(unittest.TestCase):
    def test_unpaid_is_402_with_nano_accepts(self):
        st, body = service().serve(ROOT, TEST_THR)
        self.assertEqual(st, 402)
        a = body["accepts"][0]
        self.assertEqual((a["network"], a["payTo"], a["asset"]), ("nano:mainnet", PAYTO, "XNO"))
        self.assertGreater(int(a["maxAmountRequired"]), 0)

    def test_paid_returns_valid_work_and_verifier_got_price(self):
        v = verifier()
        s = service(verify_payment=v)
        st, body = s.serve(ROOT, TEST_THR, header())
        self.assertEqual(st, 200)
        self.assertTrue(W.validates(bytes.fromhex(ROOT), body["work"], TEST_THR))
        self.assertEqual(v.calls[0][1], PAYTO)

    def test_unverified_payment_gets_no_work(self):
        st, body = service(verify_payment=verifier(False, "unconfirmed")).serve(ROOT, TEST_THR, header())
        self.assertEqual((st, body["error"]), (402, "payment_not_verified"))

    def test_one_payment_buys_one_work(self):
        s = service()
        self.assertEqual(s.serve(ROOT, TEST_THR, header())[0], 200)
        st, body = s.serve(ROOT, TEST_THR, header())
        self.assertEqual((st, body["error"]), (402, "payment_already_used"))

    def test_failed_solve_does_not_consume_payment(self):
        def bad(root, thr):
            return "0000000000000000"
        s = service(solver=bad)
        self.assertEqual(s.serve(ROOT, TEST_THR, header())[0], 503)
        s.solver = lambda r, t: W.solve_parallel(r, t, budget_seconds=30, workers=1)
        self.assertEqual(s.serve(ROOT, TEST_THR, header())[0], 200)

    def test_garbage_header_and_root(self):
        self.assertEqual(service().serve(ROOT, TEST_THR, "%%%")[0], 400)
        self.assertEqual(service().serve("zz", TEST_THR)[0], 400)
        self.assertEqual(service().serve("ab" * 31, TEST_THR)[0], 400)


class Client(unittest.TestCase):
    def setUp(self):
        self.s = service()
        self.fetch = lambda root, thr, hdr: self.s.serve(root, TEST_THR if thr == "t" else thr, hdr)

    def test_without_pay_only_quotes(self):
        r = ws.get_work(ROOT, "t", self.fetch, 10 ** 40)
        self.assertFalse(r["paid"])

    def test_refuses_over_cap_and_never_pays(self):
        paid = []
        with self.assertRaises(ws.Refused) as c:
            ws.get_work(ROOT, "t", self.fetch, 1, pay=lambda to, amt: paid.append(1))
        self.assertEqual(c.exception.code, "over_cap")
        self.assertEqual(paid, [])

    def test_pays_then_checks_the_work_itself(self):
        r = ws.get_work(ROOT, "t", self.fetch, 10 ** 40, pay=lambda to, amt: BLOCK)
        self.assertTrue(W.validates(bytes.fromhex(ROOT), r["work"], TEST_THR))

    def test_rejects_work_that_does_not_validate(self):
        def lying(root, thr, hdr):
            return (200, {"work": "0000000000000000", "threshold": hex(TEST_THR)}) if hdr else self.s.serve(root, TEST_THR)
        with self.assertRaises(ws.Refused) as c:
            ws.get_work(ROOT, "t", lying, 10 ** 40, pay=lambda to, amt: BLOCK)
        self.assertEqual(c.exception.code, "bad_work")


class Http(unittest.TestCase):
    def test_over_loopback(self):
        s = service()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), ws.make_handler(s))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/work" % srv.server_address[1]
            def post(h=None):
                req = urllib.request.Request(url, json.dumps({"hash": ROOT, "threshold": TEST_THR}).encode(),
                                             {"Content-Type": "application/json", **({"X-PAYMENT": h} if h else {})})
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        return r.status, json.load(r)
                except urllib.error.HTTPError as e:
                    return e.code, json.load(e)
            self.assertEqual(post()[0], 402)
            st, body = post(header())
            self.assertEqual(st, 200)
            self.assertTrue(W.validates(bytes.fromhex(ROOT), body["work"], TEST_THR))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
