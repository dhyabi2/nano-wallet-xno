import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tomb_check as T

A = "nano_3m8cz87zwxb1y16ob4bzp1eyek78qaig8ktohk7d45b18sh6u9exbowbnekr"
NOW = 2_000_000_000
DAY = 86400

def node(info, recv=None):
    def post(rpc, p):
        return info if p["action"] == "account_info" else (recv if recv is not None else {"blocks": ""})
    return post

class Tomb(unittest.TestCase):
    def run_(self, info, recv=None, idle=14):
        return T.check(A, idle, ["x"], node(info, recv), now=NOW)
    def test_live(self):
        c, o = self.run_({"balance": str(10**30), "modified_timestamp": str(NOW - 2 * DAY), "block_count": "3"}); self.assertEqual((c, o["verdict"]), (0, "live"))
    def test_tomb_suspect(self):
        c, o = self.run_({"balance": str(10**30), "modified_timestamp": str(NOW - 40 * DAY)}); self.assertEqual((c, o["verdict"]), (3, "tomb_suspect"))
    def test_empty_old_is_not_a_tomb(self):
        c, o = self.run_({"balance": "0", "modified_timestamp": str(NOW - 400 * DAY)}); self.assertEqual((c, o["verdict"]), (0, "empty"))
    def test_unopened_with_receivable(self):
        c, o = self.run_({"error": "Account not found"}, {"blocks": {"AB" * 32: {"amount": "5", "source": "nano_x"}}}); self.assertEqual((c, o["verdict"]), (3, "receivable_unopened"))
    def test_unopened_nothing(self):
        c, o = self.run_({"error": "Account not found"}); self.assertEqual((c, o["verdict"]), (0, "unopened"))
    def test_no_node(self):
        import urllib.error
        def boom(r, p): raise urllib.error.URLError("x")
        self.assertEqual(T.check(A, 14, ["x"], boom, now=NOW)[0], 4)
    def test_bad_address_64(self):
        self.assertEqual(T.main(["nano_notvalid"]), 64)
    def test_limit_always_printed(self):
        self.assertIn("vault", self.run_({"balance": "1", "modified_timestamp": str(NOW)})[1]["limit"])
if __name__ == "__main__": unittest.main()
