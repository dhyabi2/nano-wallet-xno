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
        self.forget = set()          # hashes this node answers "not found" for
        self.no_successor = False    # a node that does not report `successor`

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
        if block_hash in self.forget:
            self.calls.append("block_info")
            return {}
        found = super().block_info(block_hash)
        if self.no_successor:
            found.pop("successor", None)
        return found


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

    # -- the slot is read from the chain, not from one lookup -----------------

    def test_a_landed_block_this_node_cannot_find_is_not_called_lost(self):
        # The block landed and the account moved past it, but this node does
        # not answer for it (pruned, or a different node behind a balancer).
        # "Not found and the frontier moved" used to read as send_did_not_land,
        # which tells the agent to pay again under a new key: a second payment.
        self.first_attempt_fails("lose_reply")
        landed = fakenode._hash_of(self.sends_applied()[0])
        self.send(key="k2", amount="0.5")
        self.node.forget = {landed}
        out = self.send()
        self.assertEqual(out["block_hash"], landed)
        self.assertEqual(out["reconciled"], "landed")
        self.assertEqual(len(self.sends_applied()), 2)

    def test_the_successor_of_previous_settles_it_when_the_chain_cannot_be_walked(self):
        self.first_attempt_fails("lose_reply")
        landed = fakenode._hash_of(self.sends_applied()[0])
        self.send(key="k2", amount="0.5")
        self.node.forget = {landed, self.sent["k2"]["block_hash"]}
        out = self.send()
        self.assertEqual((out["block_hash"], out["reconciled"]), (landed, "landed"))

    def test_a_landed_block_is_found_by_walking_back_when_no_successor_is_reported(self):
        self.first_attempt_fails("lose_reply")
        landed = fakenode._hash_of(self.sends_applied()[0])
        self.send(key="k2", amount="0.5")
        self.send(key="k3", amount="0.5")
        self.node.forget = {landed}
        self.node.no_successor = True
        out = self.send()
        self.assertEqual(out["reconciled"], "landed")
        self.assertEqual(out["block_hash"], landed)

    def test_a_taken_slot_is_still_named_when_found_by_walking_back(self):
        self.first_attempt_fails("drop")
        self.send(key="k2", amount="0.5")
        self.send(key="k3", amount="0.5")
        self.node.no_successor = True
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "send_did_not_land")
        self.assertEqual(len(self.sends_applied()), 2)

    def test_when_the_chain_cannot_be_read_back_the_outcome_stays_unknown(self):
        self.first_attempt_fails("lose_reply")
        landed = fakenode._hash_of(self.sends_applied()[0])
        self.send(key="k2", amount="0.5")
        self.node.forget = {landed, self.sent["k2"]["block_hash"]}
        self.node.no_successor = True
        with self.assertRaises(payments.ToolError) as caught:
            self.send()
        self.assertEqual(caught.exception.reason, "send_outcome_unknown")
        self.assertEqual(len(self.sends_applied()), 2)

    # -- one block, one key ---------------------------------------------------

    def test_a_second_key_cannot_claim_the_identical_block(self):
        # Identical terms on the same frontier sign to the identical block. A
        # second key that "sent" it would report a payment that is the first
        # key's, and the agent would believe it paid twice.
        self.first_attempt_fails("drop")
        held = self.sent["k1"]["block_hash"]
        with self.assertRaises(payments.ToolError) as caught:
            self.send(key="k2")
        self.assertEqual(caught.exception.reason, "duplicate_send")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("k1", caught.exception.message)
        self.assertEqual(self.sends_applied(), [])
        self.assertNotIn("k2", self.sent)
        out = self.send()                   # the first key settles it
        self.assertEqual((out["block_hash"], out["reconciled"]), (held, "rebroadcast"))
        self.assertEqual(self.balance(), 9 * RAW)

    def test_a_block_already_held_by_another_key_is_not_settled_twice(self):
        # Records written before the refusal above existed: two keys, one hash.
        self.first_attempt_fails("lose_reply")
        self.sent["k2"] = dict(self.sent["k1"])
        first = self.send(key="k2")
        self.assertEqual(first["reconciled"], "landed")
        with self.assertRaises(payments.ToolError) as caught:
            self.send(key="k1")
        self.assertEqual(caught.exception.reason, "duplicate_send")
        self.assertEqual(len(self.sends_applied()), 1)


class AMandateRefusalLeavesNothingToRebroadcast(unittest.TestCase):
    """The mandate re-checks the cap when it reserves, just before broadcast.

    The send's own record of the signed block used to be written before that
    reservation. When the reservation refused (another process spent the cap
    while this one was finding work), the record stayed, with no result - the
    shape of a broadcast whose reply was lost. The retry then "settled" it by
    publishing that block, which the mandate had refused, past the cap.
    """

    def setUp(self):
        import json
        import shutil
        import tempfile
        import mandate as M
        self.M = M
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.keys = keystore.KeyStore()
        self.node = fakenode.FakeNode()
        self.agent = payments.create_address(store="memory", keys=self.keys)["address"]
        self.node.fund(self.agent, 10 * RAW)
        payments.receive(self.agent, self.node, self.keys)
        operator_key = bytes(range(1, 33))
        operator = M.address_from_public_key(M.public_key_from_private(operator_key))
        signed = M.sign_mandate(
            M.build_mandate(self.agent, operator, M.xno_to_raw("1"), M.xno_to_raw("1"),
                            "Pay the burn address in tests", "2030-01-01T00:00:00Z",
                            allowed_payees=[BURN]),
            operator_key)
        self.path = os.path.join(self.tmp, "mandate.json")
        with open(self.path, "w") as fh:
            json.dump(signed, fh)
        self.env = {"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_MANDATE": self.path}
        self.sent = {}

    def send(self, key="k1", amount="1"):
        return payments.send(self.agent, BURN, amount, key, self.node, self.keys,
                             self.sent, env=self.env)

    def spent(self):
        return int(self.M.MandateGuard.from_file(self.path, agent=self.agent).status()["spent_raw"])

    def sends_applied(self):
        return [b for b in self.node.published if b.get("_subtype") == "send"]

    def another_process_spends_while_work_is_found(self, amount="0.6"):
        find_work = self.node.work_generate
        other = self.M.MandateGuard.from_file(self.path, agent=self.agent)

        def work_generate(root, subtype=None):
            if subtype == "send" and not getattr(self, "_raced", False):
                self._raced = True
                other.spend(BURN, self.M.xno_to_raw(amount), lambda: None, ref="other")
            return find_work(root, subtype)
        self.node.work_generate = work_generate

    def test_a_refused_reservation_is_not_rebroadcast_on_retry(self):
        self.another_process_spends_while_work_is_found()
        with self.assertRaises(payments.ToolError) as first:
            self.send()
        self.assertEqual(first.exception.reason, "mandate_refused")
        self.assertEqual(self.sends_applied(), [])
        self.assertNotIn("k1", self.sent, "a refused send left a record a retry would publish")
        with self.assertRaises(payments.ToolError) as again:
            self.send()
        self.assertEqual(again.exception.reason, "mandate_refused")
        self.assertEqual(self.sends_applied(), [], "the refused block was published on retry")
        self.assertEqual(self.spent(), self.M.xno_to_raw("0.6"), "the cap's ledger is unchanged")

    def test_a_reserved_send_whose_reply_was_lost_is_still_settled(self):
        process = self.node.process

        def lose_reply(block):
            process(block)
            raise nanonode.NodeError("node_unreachable", "the reply timed out")
        self.node.process = lose_reply
        with self.assertRaises(payments.ToolError):
            self.send()
        self.node.process = process
        out = self.send()
        self.assertEqual(out["reconciled"], "landed")
        self.assertEqual(len(self.sends_applied()), 1)
        self.assertEqual(self.spent(), self.M.xno_to_raw("1"), "reserved once, not twice")


if __name__ == "__main__":
    unittest.main()
