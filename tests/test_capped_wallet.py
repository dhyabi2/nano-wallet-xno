"""Laws for capped_wallet: the cap is enforced in code, per call and per day, before anything is signed.

Offline: the node is fakenode.FakeNode, so no socket is opened and no real payment can run. Expected numbers are written
as plain integers of raw (1 XNO = 10**30), never derived from the code under test.
"""
import json
import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import capped_wallet as C      # noqa: E402
import fakenode                # noqa: E402
import nanonode                # noqa: E402

RAW = 10 ** 30
PAYEE = "nano_1p7cqqnfo91zwcnse54f3ucyfi6xwzsbnkt3ay14cd1g6epmiygnt7jtcoa5"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "w")
        self.node = fakenode.FakeNode()
        self.clock = Clock()
        self.w = C.CappedWallet(self.home, "0.01", "0.03", node=self.node, clock=self.clock)
        self.info = self.w.create()
        self.addr = self.info["address"]
        self.node.fund(self.addr, 5 * RAW)
        self.w.receive()

    def tearDown(self):
        self.tmp.cleanup()

    def blocks_sent(self):
        return [b for b in self.node.published if b.get("_subtype") == "send"]


class Caps(Base):
    def test_within_both_caps_pays(self):
        r = self.w.pay(PAYEE, "0.01", "k1")
        self.assertEqual(r["amount_xno"], "0.010000")
        self.assertEqual(len(self.blocks_sent()), 1)

    def test_per_call_cap_refuses_before_signing(self):
        with self.assertRaises(Exception) as cm:
            self.w.pay(PAYEE, "0.011", "k1")
        self.assertIn("0.01", str(cm.exception))            # the signer's own NANO_WALLET_MAX_SEND_XNO limit
        self.assertEqual(self.blocks_sent(), [])
        with self.assertRaises(C.Refused) as own:           # and the cap's own check, which does not rely on the signer
            self.w.cap.check(PAYEE, 11 * 10 ** 27)
        self.assertEqual(own.exception.reason, "over_per_call_cap")

    def test_daily_cap_is_a_sum_not_a_per_call_check(self):
        for i in range(3):
            self.w.pay(PAYEE, "0.01", "k%d" % i)
        with self.assertRaises(Exception) as cm:
            self.w.pay(PAYEE, "0.001", "k3")
        self.assertIn("daily", str(cm.exception))
        self.assertEqual(len(self.blocks_sent()), 3)          # exactly 0.03 XNO moved, the fourth was refused

    def test_rolling_window_releases_after_24_hours(self):
        for i in range(3):
            self.w.pay(PAYEE, "0.01", "k%d" % i)
        self.clock.t += 24 * 3600 + 1
        self.w.pay(PAYEE, "0.01", "later")
        self.assertEqual(len(self.blocks_sent()), 4)

    def test_same_idempotency_key_does_not_spend_the_cap_twice(self):
        self.w.pay(PAYEE, "0.01", "same")
        r = self.w.pay(PAYEE, "0.01", "same")
        self.assertTrue(r.get("replayed"))
        self.assertEqual(self.w.status()["spent_last_24h_xno"], "0.01")

    def test_a_send_that_raised_stays_counted(self):
        orig = self.node.process

        def lose(block):
            orig(block)
            raise nanonode.NodeError("node_unreachable", "reply lost")
        self.node.process = lose
        with self.assertRaises(Exception):
            self.w.pay(PAYEE, "0.01", "k1")
        self.assertEqual(self.w.status()["spent_last_24h_xno"], "0.01")      # may have landed: never forgotten

    def test_floats_and_garbage_refuse(self):
        for bad in (0.01, True, "0", "-1", "1e-2", "abc"):
            with self.assertRaises(Exception, msg=repr(bad)):
                self.w.pay(PAYEE, bad, "k")
        self.assertEqual(self.blocks_sent(), [])

    def test_bad_checksum_payee_refuses(self):
        with self.assertRaises(Exception):
            self.w.pay(PAYEE[:-1] + ("1" if PAYEE[-1] != "1" else "3"), "0.001", "k")
        self.assertEqual(self.blocks_sent(), [])


class Hardening(Base):
    def test_a_later_larger_cap_cannot_loosen_the_wallet(self):
        w2 = C.CappedWallet(self.home, "5", "50", node=self.node, clock=self.clock)
        with self.assertRaises(Exception):
            w2.pay(PAYEE, "0.02", "k")
        self.assertEqual(w2.status()["per_call_max_xno"], "0.01")

    def test_a_later_smaller_cap_does_tighten(self):
        w2 = C.CappedWallet(self.home, "0.001", "0.002", node=self.node, clock=self.clock)
        self.assertEqual(w2.status()["per_day_max_xno"], "0.002")

    def test_corrupt_ledger_fails_closed(self):
        self.w.pay(PAYEE, "0.01", "k1")
        with open(os.path.join(self.home, "ledger.json"), "w") as fh:
            fh.write("{not json")
        with self.assertRaises(Exception) as cm:
            self.w.pay(PAYEE, "0.001", "k2")
        self.assertIn("ledger", str(cm.exception))
        self.assertEqual(len(self.blocks_sent()), 1)

    def test_corrupt_caps_file_refuses_to_run_uncapped(self):
        with open(os.path.join(self.home, "caps.json"), "w") as fh:
            fh.write("garbage")
        with self.assertRaises(C.Refused):
            C.CappedWallet(self.home, "0.01", "0.03", node=self.node)

    def test_clock_going_backwards_refuses(self):
        self.w.pay(PAYEE, "0.01", "k1")
        self.clock.t -= 3600
        with self.assertRaises(Exception) as cm:
            self.w.pay(PAYEE, "0.001", "k2")
        self.assertIn("clock", str(cm.exception))

    def test_key_file_is_private_never_overwritten_and_never_returned(self):
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, "key.json")).st_mode)
        self.assertEqual(mode & 0o077, 0)
        with open(os.path.join(self.home, "key.json")) as fh:
            key_hex = fh.read().strip()
        with self.assertRaises(C.Refused):
            self.w.create()
        blob = json.dumps([self.info, self.w.status(), self.w.balance()])
        for t in self.w.tools():
            blob += json.dumps({k: v for k, v in t.items() if k != "fn"})
            if not t["parameters"]["required"]:
                blob += json.dumps(t["fn"]())
        self.assertNotIn(key_hex, blob)
        self.assertNotIn(key_hex.lower(), blob.lower())

    def test_caps_must_be_ordered(self):
        with self.assertRaises(C.Refused):
            C.CappedWallet(os.path.join(self.tmp.name, "x"), "0.5", "0.1")


class Tools(Base):
    def test_no_tool_takes_a_cap_key_or_node_parameter(self):
        names = {t["name"] for t in self.w.tools()}
        self.assertEqual(names, {"wallet_address", "wallet_status", "wallet_balance", "wallet_pay"})
        for t in self.w.tools():
            props = set(t["parameters"]["properties"])
            self.assertFalse(props & {"cap", "per_call", "per_day", "limit", "key", "key_path", "node", "url"}, t["name"])

    def test_pay_tool_returns_a_refusal_not_an_exception(self):
        pay = next(t for t in self.w.tools() if t["name"] == "wallet_pay")["fn"]
        out = pay(payee=PAYEE, amount_xno="0.02", idempotency_key="k")
        self.assertFalse(out["ok"])
        self.assertEqual(self.blocks_sent(), [])
        ok = pay(payee=PAYEE, amount_xno="0.005", idempotency_key="k2")
        self.assertTrue(ok["ok"])

    def test_cli_pay_is_a_dry_run_by_default(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = C.main(["--home", self.home, "pay", PAYEE, "0.005", "--key", "x"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(buf.getvalue())["dry_run"])
        self.assertEqual(self.blocks_sent(), [])


if __name__ == "__main__":
    unittest.main()
