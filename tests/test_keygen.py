"""nano_keygen.py: the one-file generator. It must agree with the full wallet, never
leak the seed to stdout, and never touch the network."""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import nano_keygen as kg
import nanoaddr
import wallet

KEYGEN = os.path.join(ROOT, "nano_keygen.py")


def run(*args, env=None):
    return subprocess.run([sys.executable, "-I", KEYGEN, *args], capture_output=True,
                          text=True, env=env, timeout=60)


class Keygen(unittest.TestCase):
    def test_official_vector(self):
        self.assertEqual(kg.selftest(), [])
        self.assertEqual(run("selftest").returncode, 0)

    def test_agrees_with_the_full_wallet_on_random_seeds(self):
        for _ in range(5):
            seed = os.urandom(32)
            for index in (0, 1, 7):
                mine = kg.account(seed, index)
                full = wallet.derive_account(seed.hex(), index)
                self.assertEqual(mine["address"], full["address"])
                self.assertEqual(mine["public_key"], full["public_key"])

    def test_check_agrees_with_nanoaddr_on_every_single_character_corruption(self):
        address = kg.account(os.urandom(32))["address"]
        alphabet = kg._ALPHABET
        for i in range(5, len(address)):
            for c in (alphabet[0], alphabet[-1], "0", "l"):
                if c == address[i]:
                    continue
                bad = address[:i] + c + address[i + 1:]
                self.assertEqual(kg.check(bad)["valid"], nanoaddr.is_valid(bad), bad)

    def test_a_bad_checksum_is_refused_with_exit_1(self):
        good = kg.VECTOR_ADDRESS
        bad = good[:-1] + ("1" if good[-1] != "1" else "3")
        self.assertEqual(run("check", good).returncode, 0)
        out = run("check", bad)
        self.assertEqual(out.returncode, 1)
        self.assertIn("bad_checksum", out.stdout)

    def test_seed_goes_to_stderr_only(self):
        out = run("new")
        self.assertEqual(out.returncode, 0)
        address = out.stdout.strip()
        self.assertTrue(kg.check(address)["valid"])
        self.assertIsNone(re.search(r"[0-9A-Fa-f]{64}", out.stdout), "stdout must carry no 64-hex value")
        seed = re.search(r"\b([0-9A-F]{64})\b", out.stderr).group(1)
        self.assertEqual(kg.account(bytes.fromhex(seed))["address"], address)

    def test_seed_file_is_0600_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seed")
            out = run("new", "--seed-file", path)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertNotIn(open(path).read().strip(), out.stdout + out.stderr)
            before = open(path).read()
            again = run("new", "--seed-file", path)
            self.assertEqual(again.returncode, 2)
            self.assertEqual(open(path).read(), before)
            self.assertEqual(run("address", before.strip()).stdout.strip(), out.stdout.strip())

    def test_the_file_cannot_reach_the_network(self):
        with open(KEYGEN, encoding="utf-8") as f:
            source = f.read()
        for module in ("socket", "urllib", "http", "ssl", "requests", "asyncio", "subprocess"):
            self.assertIsNone(re.search(r"^\s*(import|from)\s+%s\b" % module, source, re.M), module)


if __name__ == "__main__":
    unittest.main()
