"""Local proof-of-work, pinned to a block the live network actually accepted.

The validator has one job and one way to get it wrong: byte order. So the
vector below is not constructed here - it is read off
`BCE621224274F7DCB1B9BFB212CF9E5CADC50B47E8BEA5C2E4ED0F063566D85B`, a real
open block on the Nano mainnet (0.0005 XNO, the receive that issue #6
recorded). Its work was accepted by real nodes, so any implementation that
calls it invalid is wrong no matter what else passes. Of the six plausible
orderings of (work, root) x (little, big) exactly one validates it.

That same vector also pins both thresholds at once: a receive block's work
must clear the receive threshold and must NOT clear the send threshold,
because Nano asks 64x more of a send. Nothing here opens a socket.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import work  # noqa: E402

# The real open block, from the live network.
REAL_ROOT = bytes.fromhex(
    "8789CF4B88407CB006F5A53F6FEEECFCDC6E56F8AE51B9BBF806317A33643490")
REAL_WORK = "58fc8abfa35fcfee"

EXPIRED = lambda: 1e9  # noqa: E731 - a clock already past any deadline


class TheRealNetworksOwnWork(unittest.TestCase):
    def test_a_block_the_network_accepted_validates_here(self):
        self.assertTrue(work.validates(REAL_ROOT, REAL_WORK),
                        "the validator rejects work the Nano network accepted")

    def test_it_does_not_reach_the_send_threshold(self):
        # It is a receive block. If this ever passes, the two thresholds have
        # been confused and sends would go out under-worked.
        self.assertFalse(work.validates(REAL_ROOT, REAL_WORK, work.SEND_THRESHOLD))

    def test_the_send_threshold_is_the_harder_one(self):
        self.assertGreater(work.SEND_THRESHOLD, work.RECEIVE_THRESHOLD)

    def test_one_flipped_nibble_is_not_valid_work(self):
        self.assertFalse(work.validates(REAL_ROOT, "58fc8abfa35fcfef"))

    def test_valid_work_belongs_to_its_own_root_only(self):
        self.assertFalse(work.validates(bytes(32), REAL_WORK))

    def test_malformed_work_is_invalid_not_an_exception(self):
        for bad in ("", "ff", "58fc8abfa35fcfeeff", "zzzzzzzzzzzzzzzz"):
            self.assertFalse(work.validates(REAL_ROOT, bad), bad)

    def test_difficulty_names_a_bad_length(self):
        with self.assertRaises(ValueError):
            work.difficulty(REAL_ROOT, "ff")
        with self.assertRaises(ValueError):
            work.difficulty(b"short", REAL_WORK)


class Solving(unittest.TestCase):
    def test_work_it_finds_validates_at_the_threshold_asked_for(self):
        # An easy threshold: the search and the encoding are the same code at
        # any threshold, and the real one averages 2**23 hashes.
        easy = 0x8000000000000000
        found = work.solve(REAL_ROOT, easy)
        self.assertEqual(len(found), 16)
        self.assertTrue(work.validates(REAL_ROOT, found, easy))

    def test_what_it_returns_round_trips_through_difficulty(self):
        found = work.solve(REAL_ROOT, 0)
        self.assertGreaterEqual(work.difficulty(REAL_ROOT, found), 0)
        bytes.fromhex(found)  # it is hex, and 8 bytes of it

    def test_it_finds_work_for_a_root_it_has_never_seen(self):
        easy = 0x8000000000000000
        root = bytes(range(32))
        self.assertTrue(work.validates(root, work.solve(root, easy), easy))

    def test_it_refuses_the_send_threshold_rather_than_spend_minutes(self):
        with self.assertRaises(work.WorkUnavailable) as caught:
            work.solve(REAL_ROOT, work.SEND_THRESHOLD, _now=EXPIRED)
        self.assertIn("receive threshold", str(caught.exception))

    def test_an_exhausted_budget_gives_up_without_hashing(self):
        # Checked before the first hash, so this is deterministic rather than
        # a 1-in-1000 race against finding work early.
        with self.assertRaises(work.WorkUnavailable) as caught:
            work.solve(REAL_ROOT, work.RECEIVE_THRESHOLD, budget_seconds=0.0, _now=EXPIRED)
        self.assertIn("0 attempts", str(caught.exception))

    def test_a_bad_root_is_named(self):
        with self.assertRaises(ValueError):
            work.solve(b"short")

    def test_only_receive_and_open_are_local_subtypes(self):
        self.assertEqual(work.LOCAL_SUBTYPES, frozenset({"receive", "open"}))
        for spending in ("send", "change", "epoch", None, ""):
            self.assertNotIn(spending, work.LOCAL_SUBTYPES)


if __name__ == "__main__":
    unittest.main()
