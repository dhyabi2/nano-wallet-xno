import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import identity_address as ia, nano_keygen as nk

S = bytes(range(32))


class T(unittest.TestCase):
    def test_valid_checksummed_and_stable(self):
        a = ia.address_for(S)
        self.assertTrue(nk.check(a["address"])["valid"])
        self.assertEqual(a, ia.address_for(S))

    def test_different_secret_different_address(self):
        self.assertNotEqual(ia.address_for(S)["address"], ia.address_for(bytes(reversed(S)))["address"])

    def test_naive_reencoding_differs_from_derived(self):
        self.assertNotEqual(ia.naive_address(S), ia.address_for(S)["address"])

    def test_naive_public_key_is_not_the_nano_public_key_of_same_secret(self):
        self.assertNotEqual(nk.check(ia.naive_address(S))["public_key"], nk.public_key(S).hex().upper())

    def test_output_never_contains_secret_or_seed(self):
        out = str(ia.address_for(S)).lower()
        self.assertNotIn(S.hex(), out)
        self.assertNotIn(ia.nano_seed(S).hex(), out)

    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError): ia.nano_seed(b"x")


if __name__ == "__main__":
    unittest.main()
