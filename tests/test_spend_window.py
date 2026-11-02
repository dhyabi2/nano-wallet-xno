"""Laws for spend_window: the allowance is the balance, caps are enforced in code, nothing here can send.

Expected numbers are plain integers of raw (1 XNO = 10**30), not derived from the code under test.
"""
import ast
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import spend_window as sw  # noqa: E402

ADDR = "nano_39b311tznm9hub316enn8159ammduxnfonoaei7afzymw66wsouw9q3j9ic1"
PAYEE = "nano_1p7cqqnfo91zwcnse54f3ucyfi6xwzsbnkt3ay14cd1g6epmiygnt7jtcoa5"
OTHER = "nano_1yo6c1t64ahfjdw1dxizmbbnpdmbrckwhw9phbg5pdkeubrizga4qhnjmnx7"
BAD = ADDR[:-1] + ("a" if ADDR[-1] != "a" else "b")  # fails its checksum
CEIL = 50_000_000_000_000_000_000_000_000_000  # 0.05 XNO
NOW = "2026-10-25T06:00:00Z"
H1, H2, H3 = "A" * 64, "B" * 64, "C" * 64


def opened(state=None, **over):
    state = state if state is not None else {"spend_windows": []}
    kw = dict(purpose="settle foreign claims", ceiling_raw=str(CEIL), address=ADDR, approved_by="ops@example.org",
              approved_at="2026-10-25T05:00:00Z", approval_url="https://example.org/approvals/1",
              expires="2026-11-25T00:00:00Z", now=NOW)
    kw.update(over)
    return state, sw.open_window(state, **kw)


def refuses(code, fn, *a, **k):
    with unittest.TestCase().assertRaises(sw.WindowError) as cm:
        fn(*a, **k)
    unittest.TestCase().assertEqual(cm.exception.code, code)


class Laws(unittest.TestCase):
    def test_1_two_records_reach_the_ceiling_exactly_and_close_as_exhausted(self):
        st, w = opened()
        half = CEIL // 2
        sw.record_send(st, w["id"], block=H1, amount_raw=str(half), payee=PAYEE, now=NOW)
        v = sw.record_send(st, w["id"], block=H2, amount_raw=str(CEIL - half), payee=PAYEE, now=NOW)
        self.assertEqual((v["remaining_raw"], v["spent_raw"], v["remaining_xno"], v["spent_xno"]), ("0", str(CEIL), "0", "0.05"))
        self.assertEqual(sw.close_window(st, w["id"], reason="exhausted", now=NOW)["state"], "closed")

    def test_2_one_raw_over_refuses_and_leaves_the_file_byte_identical(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.json")
        st, w = opened()
        sw.record_send(st, w["id"], block=H1, amount_raw=str(CEIL - 5), payee=PAYEE, now=NOW)
        sw.save(st, p)
        before = open(p, "rb").read()
        argv = ["--state", p, "record", w["id"], "--block", H2, "--amount-raw", "6", "--payee", PAYEE, "--now", NOW]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sw.main(argv)
        self.assertEqual((rc, json.loads(buf.getvalue())["error"]), (2, "would_exceed_ceiling"))
        self.assertEqual(open(p, "rb").read(), before)

    def test_3_a_decimal_or_float_ceiling_refuses(self):
        for bad in ("0.05", 0.05, "5e28", "-1", "", True):
            refuses("ceiling_not_integer_string", opened, ceiling_raw=bad)

    def test_4_no_citable_approval_refuses(self):
        for bad in (None, "", "ops said so", "ftp://x/y", "/local/path"):
            refuses("approval_not_citable", opened, approval_url=bad)

    def test_5_expiry_must_exist_and_be_after_now(self):
        refuses("no_expiry", opened, expires=None)
        refuses("expiry_not_after_now", opened, expires=NOW)
        refuses("expiry_not_after_now", opened, expires="2026-10-25T05:59:59Z")

    def test_6_revoking_without_a_sweep_block_refuses(self):
        st, w = opened()
        refuses("revocation_without_sweep", sw.close_window, st, w["id"], reason="revoked", now=NOW)
        self.assertEqual(sw.close_window(st, w["id"], reason="revoked", now=NOW, sweep_block=H3)["sweep_block"], H3)

    def test_7_the_two_literal_claims_hold_on_every_row_and_in_the_feed(self):
        st, w = opened()
        opened(st, purpose="second", now="2026-10-25T07:00:00Z")
        f = sw.feed(st, NOW)
        self.assertTrue(all(r["the_allowance_is_the_balance"] is True for r in f["windows"]))
        self.assertIs(f["cached_approvals_outstanding"], 0)
        self.assertEqual([r["id"] for r in f["windows"]], ["sw-2026-10-25-001", "sw-2026-10-25-002"])

    def test_8_a_failing_checksum_refuses(self):
        refuses("bad_address_checksum", opened, address=BAD)
        st, w = opened()
        refuses("bad_address_checksum", sw.record_send, st, w["id"], block=H1, amount_raw="1", payee=BAD, now=NOW)

    def test_9_byte_rule_honest_when_empty_and_no_socket(self):
        empty = sw.feed({"spend_windows": []}, NOW)
        self.assertEqual((empty["open_windows"], empty["ceiling_total_raw"], empty["windows"]), (0, "0", []))
        st, _ = opened()
        self.assertEqual(sw.dump(sw.feed(st, NOW)), sw.dump(sw.feed(json.loads(sw.dump(st)), NOW)))
        with open(os.path.join(ROOT, "spend_window.py")) as fh:
            tree = ast.parse(fh.read())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(names, {"argparse", "json", "re", "sys", "nanoaddr"})
        tree.body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
        src = ast.unparse(tree)
        for word in ("socket", "urllib", "sign(", "float("):
            self.assertNotIn(word, src)

    def test_10_no_clock_refuses(self):
        refuses("no_clock", opened, now=None)

    def test_11_closed_window_takes_no_more_records(self):
        st, w = opened()
        sw.close_window(st, w["id"], reason="revoked", now=NOW, sweep_block=H3)
        refuses("window_closed", sw.record_send, st, w["id"], block=H1, amount_raw="1", payee=PAYEE, now=NOW)

    def test_12_the_same_block_is_never_counted_twice(self):
        st, w = opened()
        sw.record_send(st, w["id"], block=H1, amount_raw="5", payee=PAYEE, now=NOW)
        refuses("block_already_recorded", sw.record_send, st, w["id"], block=H1.lower(), amount_raw="5", payee=PAYEE, now=NOW)

    def test_13_payee_allow_list_and_per_call_cap_are_enforced_in_code(self):
        st, w = opened(allowed_payees=[PAYEE], per_call_cap_raw="1000")
        refuses("payee_not_allowed", sw.record_send, st, w["id"], block=H1, amount_raw="1", payee=OTHER, now=NOW)
        refuses("above_per_call_cap", sw.record_send, st, w["id"], block=H1, amount_raw="1001", payee=PAYEE, now=NOW)
        self.assertEqual(sw.record_send(st, w["id"], block=H1, amount_raw="1000", payee=PAYEE, now=NOW)["spent_raw"], "1000")

    def test_14_exhausted_and_expired_cannot_be_claimed_falsely(self):
        st, w = opened()
        refuses("not_exhausted", sw.close_window, st, w["id"], reason="exhausted", now=NOW)
        refuses("not_expired", sw.close_window, st, w["id"], reason="expired", now=NOW)
        self.assertEqual(sw.close_window(st, w["id"], reason="expired", now="2026-11-25T00:00:00Z")["closed_reason"], "expired")

    def test_15_a_lapsed_open_window_is_counted_apart_in_the_feed(self):
        st, _ = opened()
        f = sw.feed(st, "2026-12-01T00:00:00Z")
        self.assertEqual((f["open_windows"], f["lapsed_but_not_closed"]), (0, 1))
        self.assertEqual(f["lapsed_but_not_closed_as_of"], "2026-12-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
