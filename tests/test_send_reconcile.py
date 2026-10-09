"""A send whose outcome is unknown is looked up, not left unknown for ever.

#12 made the retry of a broadcast whose reply was lost a refusal
(`send_outcome_unknown`) instead of a second payment. That stops the double
pay, but it leaves the agent holding a key it can do nothing with: the
refusal says "look that block up on the ledger", and the wallet has no call
to look it up with. An agent that cannot tell "landed" from "never sent"
either waits for ever or retries under a new key and risks paying twice.

The retry now settles it from the ledger, using only facts that cannot be
confused with each other:

  * the block is on the ledger                  -> the payment is made; the
    retry returns it, `reconciled: "landed"`.
  * it is not, and the account's frontier is
    still the block's `previous`                -> it can still be the next
    block. The SAME signed block is published again (same hash, so it can
    only ever be one payment); `reconciled: "rebroadcast"`.
  * it is not, and the frontier has moved to
    some other block                            -> the slot it named is taken;
    it can never land. `send_did_not_land`: retry under a NEW key.
  * the lookup itself fails                     -> still `send_outcome_unknown`.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fakenode                                                   # noqa: E402
import keystore                                                   # noqa: E402
import nanonode                                                   # noqa: E402
import payments                                                   # noqa: E402

RAW = 10 ** 30
BURN = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"


class FlakyNode(fakenode.FakeNode):
    """`mode` decides what the next send broadcast does:

    "lose_reply" - the network applies the block, the reply is lost.
    "drop"       - the block never reaches the network, the reply is lost.
    None         - an ordinary node.
    `lookups_fail` makes block_info unreachable.
    """

    def __init__(self):
        super().__init__()
        self.mode = None
        self.lookups_fail = False

    def process(self, block):
        if self.mode == "drop":
            self.calls.append("process")
            raise nanonode.NodeError("node_unreachable", "the reply timed out")
        block_hash = super().process(block)
        if self.mode == "lose_reply":
            raise nanonode.NodeError("node_unreachable", "the reply timed out")
        return block_hash

    def block_info(self, block_hash):
        if self.lookups_fail:
            self.calls.append("block_info")
            raise nanonode.NodeError("node_unreachable", "the lookup timed out")
        return super().block_info(block_hash)


class AnUnknownSendIsSettledFromTheLedger(unittest.TestCase):

    def setUp(self):
        self.keys = keystore.KeyStore()
        self.node = FlakyNode()
        self.agent = payments.create_address(store="memory", keys=self.keys)["address"]
        self.node.fund(self.agent, 10 * RAW)
        payments.receive(self.agent, self.node, self.keys)
        self.sent = {}
        self.env = {"NANO_WALLET_ALLOW_SEND": "1"}

    def send(self, key="k1", amount="1"):
        return payments.send(self.agent, BURN, amount, key, self.node, self.keys,
                             self.sent, env=self.env)

    def first_attempt_fails(self, mode):
        self.node.mode = mode
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "node_unreachable")
        self.node.mode = None

    def sends_applied(self):
        return [b for b in self.node.published if b.get("_subtype") == "send"]

    def balance(self):
        return self.node.accounts[self.agent]["balance_raw"]

    def test_a_block_that_landed_is_returned_as_the_payment(self):
        self.first_attempt_fails("lose_reply")
        landed = fakenode._hash_of(self.sends_applied()[0])
        out = self.send()
        self.assertEqual(out["block_hash"], landed)
        self.assertTrue(out["replayed"])
        self.assertEqual(out["reconciled"], "landed")
        self.assertTrue(out["confirmed"], "the ledger's own answer, not a guess")
        self.assertEqual(len(self.sends_applied()), 1)
        self.assertEqual(self.balance(), 9 * RAW)
        again = self.send()                 # settled: an ordinary replay now
        self.assertEqual(again["block_hash"], landed)
        self.assertEqual(len(self.sends_applied()), 1)

    def test_a_block_that_never_arrived_is_published_again_unchanged(self):
        self.first_attempt_fails("drop")
        self.assertEqual(self.sends_applied(), [])
        signed_hash = self.sent["k1"]["block_hash"]
        out = self.send()
        self.assertEqual(out["reconciled"], "rebroadcast")
        self.assertEqual(out["block_hash"], signed_hash, "the same block, not a new one")
        self.assertEqual(len(self.sends_applied()), 1)
        self.assertEqual(self.balance(), 9 * RAW, "paid exactly once")

    def test_a_block_whose_slot_was_taken_can_never_land(self):
        self.first_attempt_fails("drop")
        # A new key builds a DIFFERENT block on the same frontier. (Identical
        # terms on the same frontier would be the identical block, one hash.)
        self.send(key="k2", amount="0.5")
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "send_did_not_land")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.sends_applied()), 1)
        self.assertEqual(self.balance(), 9 * RAW + RAW // 2, "only k2 paid")
        with self.assertRaises(payments.ToolError) as again:
            self.send()                     # asked again, the same answer; nothing published
        self.assertEqual(len(self.sends_applied()), 1)
        self.assertEqual(again.exception.reason, "send_did_not_land")

    def test_a_block_that_landed_under_later_blocks_is_still_found(self):
        self.first_attempt_fails("lose_reply")
        landed = fakenode._hash_of(self.sends_applied()[0])
        self.send(key="k2", amount="0.5")   # the account moves past it
        out = self.send()
        self.assertEqual(out["block_hash"], landed)
        self.assertEqual(out["reconciled"], "landed")
        self.assertEqual(len(self.sends_applied()), 2)

    def test_when_the_lookup_fails_the_outcome_stays_unknown(self):
        self.first_attempt_fails("lose_reply")
        self.node.lookups_fail = True
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "send_outcome_unknown")
        self.assertIn(self.sent["k1"]["block_hash"], caught.exception.message)
        self.assertEqual(len(self.sends_applied()), 1)

    def test_a_rebroadcast_that_fails_again_stays_unknown(self):
        self.first_attempt_fails("drop")
        self.node.mode = "drop"
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "send_outcome_unknown")
        self.node.mode = None
        out = self.send()
        self.assertEqual(out["reconciled"], "rebroadcast")
        self.assertEqual(self.balance(), 9 * RAW)

    def test_a_reconciled_retry_with_different_terms_is_still_a_conflict(self):
        self.first_attempt_fails("drop")
        with self.assertRaises(payments.ToolError) as caught:
            self.send(amount="0.5")
        self.assertEqual(caught.exception.reason, "idempotency_conflict")
        self.assertEqual(self.sends_applied(), [])

    def test_the_rebroadcast_needs_send_enabled(self):
        self.first_attempt_fails("drop")
        self.env = {}
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "spend_not_enabled")
        self.assertEqual(self.sends_applied(), [])


if __name__ == "__main__":
    unittest.main()
