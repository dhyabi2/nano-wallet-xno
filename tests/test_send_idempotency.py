"""A broadcast whose reply never arrived must not be replayed as a second send.

Nano has no fee and no reversal. `node.process` raising says the *reply* is
missing, not the block: the network may well have accepted it. `send` recorded
the idempotency key only after a reply came back, so the retry that a 503
invites rebuilt the block from the account's NEW frontier and paid the
destination a second time under the one key that exists to stop exactly that.

Measured on the tree before the fix, with a node that applies the block and
then loses the reply:

    attempt 1: refused node_unreachable
    attempt 2: sent, replayed=False
    blocks the node accepted: 2
    XNO actually moved:       2        <- the agent asked to send 1
    idempotency keys used:    1
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


class LosesTheReply(fakenode.FakeNode):
    """Applies the block exactly as a node would, then loses the reply.

    `lose_replies` is armed after the account has been funded and opened, so
    only the sends under test lose their reply.
    """

    def __init__(self):
        super().__init__()
        self.lose_replies = False
        self.lookups_fail = False
        self.process_calls = 0

    def block_info(self, block_hash):
        if self.lookups_fail:
            raise nanonode.NodeError("node_unreachable", "the lookup timed out")
        return super().block_info(block_hash)

    def process(self, block):
        self.process_calls += 1
        block_hash = super().process(block)          # the network accepted it
        if self.lose_replies:
            raise nanonode.NodeError("node_unreachable", "the reply timed out")
        return block_hash


class ABroadcastWhoseReplyWasLost(unittest.TestCase):

    def setUp(self):
        self.keys = keystore.KeyStore()
        self.node = LosesTheReply()
        self.agent = payments.create_address(store="memory", keys=self.keys)["address"]
        self.node.fund(self.agent, 10 * RAW)
        payments.receive(self.agent, self.node, self.keys)
        self.node.lose_replies = True
        self.sent = {}
        self.env = {"NANO_WALLET_ALLOW_SEND": "1"}

    def send(self, amount="1", key="one-and-only-key", to=BURN):
        return payments.send(self.agent, to, amount, key, self.node, self.keys,
                             self.sent, env=self.env)

    def sends_published(self):
        return [b for b in self.node.published if b.get("_subtype") == "send"]

    def test_the_retry_returns_the_landed_block_rather_than_paying_a_second_time(self):
        with self.assertRaises(payments.ToolError) as first:
            self.send()
        self.assertEqual(first.exception.reason, "node_unreachable")
        published = self.sends_published()[0]
        second = self.send()        # settled from the ledger: see test_send_reconcile
        self.assertTrue(second["replayed"])
        self.assertEqual(second["block_hash"], fakenode._hash_of(published))
        self.assertEqual(len(self.sends_published()), 1, "the destination must be paid once")

    def test_when_it_cannot_be_looked_up_the_retry_is_refused_naming_the_block(self):
        with self.assertRaises(payments.ToolError):
            self.send()
        published = self.sends_published()[0]
        # The account moves past it, so the frontier alone cannot settle it
        # and the block has to be looked up - and the lookup fails.
        self.node.lose_replies = False
        self.send(amount="0.5", key="another-key")
        self.node.lookups_fail = True
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "send_outcome_unknown")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn(fakenode._hash_of(published), caught.exception.message)
        self.assertEqual(len(self.sends_published()), 2)

    def test_a_third_attempt_pays_nothing_more(self):
        with self.assertRaises(payments.ToolError):
            self.send()
        for _ in range(2):
            self.assertTrue(self.send()["replayed"])
        self.assertEqual(len(self.sends_published()), 1)

    # -- controls: these must hold with or without the fix ------------------

    def test_a_send_whose_reply_arrived_still_replays(self):
        self.node.lose_replies = False
        first = self.send()
        self.assertFalse(first.get("replayed", False))
        again = self.send()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["block_hash"], first["block_hash"])
        self.assertEqual(len(self.sends_published()), 1)

    def test_a_different_amount_under_the_same_key_is_still_a_conflict(self):
        with self.assertRaises(payments.ToolError):
            self.send()
        with self.assertRaises(payments.ToolError) as caught:
            self.send(amount="0.5")
        self.assertEqual(caught.exception.reason, "idempotency_conflict")

    def test_a_fresh_key_after_a_lost_reply_is_allowed(self):
        # The refusal is about the one key whose outcome is unknown, not about
        # the account: the wallet is not bricked by a timeout.
        with self.assertRaises(payments.ToolError):
            self.send()
        self.node.lose_replies = False
        out = self.send(key="a-new-key")
        self.assertEqual(out["amount_xno"], "1.000000")
        self.assertEqual(len(self.sends_published()), 2)


class UnderAnOperatorMandate(unittest.TestCase):
    """The mandate ledger counts an unknown broadcast as spent; it does not stop
    the replay, because the replay is a different payment under the same key."""

    def setUp(self):
        self.keys = keystore.KeyStore()
        self.node = LosesTheReply()
        self.agent = payments.create_address(store="memory", keys=self.keys)["address"]
        self.node.fund(self.agent, 10 * RAW)
        payments.receive(self.agent, self.node, self.keys)
        self.node.lose_replies = True
        self.sent = {}

    def test_the_retry_is_settled_before_the_ledger_is_touched_again(self):
        env = {"NANO_WALLET_ALLOW_SEND": "1"}
        with self.assertRaises(payments.ToolError):
            payments.send(self.agent, BURN, "1", "k", self.node, self.keys, self.sent, env=env)
        out = payments.send(self.agent, BURN, "1", "k", self.node, self.keys, self.sent, env=env)
        self.assertTrue(out["replayed"])
        self.assertEqual(self.node.process_calls, 2)   # the open, then the one send


if __name__ == "__main__":
    unittest.main()
