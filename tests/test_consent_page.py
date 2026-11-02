"""The operator consent page's logic is plain JavaScript (docs/consent.js); this runs its checks under node when node exists."""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConsentPage(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_consent_js_checks_pass(self):
        r = subprocess.run(["node", os.path.join(ROOT, "tests", "consent_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("all passed", r.stdout)

    def test_the_page_loads_only_its_own_script(self):
        with open(os.path.join(ROOT, "docs", "operator-consent.html")) as fh:
            html = fh.read()
        self.assertIn('<script src="consent.js">', html)
        self.assertNotIn("http://", html.replace("https://github.com/dhyabi2/nano-wallet-xno", ""))
        self.assertEqual(html.count("<script src="), 1)


if __name__ == "__main__":
    unittest.main()
