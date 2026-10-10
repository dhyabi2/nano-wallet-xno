"""HttpNanoNode against answers shaped the way a real public node sends them.

The fake node in `fakenode.py` stands in for `NanoNode` itself, so it never
exercises how `HttpNanoNode` reads the node's JSON. That is where a brand-new
wallet broke: a real node answers `account_info` for an account that has
never received with `{"error": "Account not found"}`, and `_rpc` raised it
as `node_error` - so `balance` and `receive` failed for every new wallet,
the one state every new user starts in. No socket is opened here either:
`urlopen` is replaced for the duration of each test.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keystore as _keystore  # noqa: E402
import nanoaddr as _nanoaddr  # noqa: E402
import nanonode  # noqa: E402
import payments  # noqa: E402
import work  # noqa: E402

NEW = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _node_answering(answer, seen):
    def urlopen(request, timeout=None):
        seen.append(request)
        return _Answer(json.dumps(answer).encode("utf-8"))
    return urlopen


class HttpNodeAgainstRealAnswers(unittest.TestCase):
    def _node(self, answer):
        seen = []
        patch = mock.patch.object(urllib.request, "urlopen", _node_answering(answer, seen))
        patch.start()
        self.addCleanup(patch.stop)
        return nanonode.HttpNanoNode("https://user:key@node.example/rpc"), seen

    def test_a_never_opened_account_is_empty_not_an_error(self):
        node, _ = self._node({"error": "Account not found"})
        self.assertEqual(node.account_info(NEW), {})

    def test_other_node_errors_still_raise_without_credentials(self):
        node, _ = self._node({"error": "Bad account number"})
        with self.assertRaises(nanonode.NodeError) as caught:
            node.account_info(NEW)
        self.assertEqual(caught.exception.reason, "node_error")
        self.assertNotIn("key", caught.exception.message)

    def test_account_not_found_is_only_forgiven_for_account_info(self):
        node, _ = self._node({"error": "Account not found"})
        with self.assertRaises(nanonode.NodeError):
            node.process({"type": "state", "_subtype": "send"})

    def test_a_block_the_node_does_not_have_is_empty_not_an_error(self):
        node, _ = self._node({"error": "Block not found"})
        self.assertEqual(node.block_info("A" * 64), {})

    def test_block_not_found_is_only_forgiven_for_block_info(self):
        node, _ = self._node({"error": "Block not found"})
        with self.assertRaises(nanonode.NodeError):
            node.account_info(NEW)

    def test_a_block_on_the_ledger_names_its_account_and_confirmation(self):
        node, seen = self._node({"block_account": NEW, "confirmed": "true",
                                 "contents": {"type": "state"}})
        self.assertEqual(node.block_info("A" * 64), {"account": NEW, "confirmed": True})
        self.assertEqual(json.loads(seen[-1].data)["action"], "block_info")

    def test_a_block_on_the_ledger_names_the_blocks_either_side_of_it(self):
        node, _ = self._node({"block_account": NEW, "confirmed": "false",
                              "successor": "b" * 64,
                              "contents": {"type": "state", "previous": "a" * 64}})
        self.assertEqual(node.block_info("C" * 64),
                         {"account": NEW, "confirmed": False,
                          "previous": "A" * 64, "successor": "B" * 64})

    def test_a_zero_successor_is_no_successor(self):
        node, _ = self._node({"block_account": NEW, "confirmed": "true",
                              "successor": "0" * 64,
                              "contents": {"type": "state", "previous": "not a hash"}})
        self.assertEqual(node.block_info("C" * 64), {"account": NEW, "confirmed": True})

    def test_process_tells_the_node_the_block_subtype(self):
        # A receive built on a stale balance is rejected by the node rather than
        # published, but only because the node is told which direction the block
        # claims to go in and can check it against the balance change. That
        # protection is what `payments.receive` relies on instead of a refusal
        # of its own, so it is pinned here.
        node, seen = self._node({"hash": "C" * 64})
        node.process({"type": "state", "balance": "1", "_subtype": "receive"})
        body = json.loads(seen[-1].data.decode("utf-8"))
        self.assertEqual(body["subtype"], "receive")
        self.assertNotIn("_subtype", body["block"])

    def test_a_block_the_node_rejects_as_invalid_is_a_rejection(self):
        # The node read the block and said it can never be valid: not a lost
        # reply, so the wallet may build a new one instead of holding this one.
        for error in ("Block work is less than threshold", "Bad signature",
                      "Balance and amount delta do not match", "Negative spend",
                      "Block is invalid"):
            node, _ = self._node({"error": error})
            with self.assertRaises(nanonode.NodeError) as caught:
                node.process({"type": "state", "_subtype": "send"})
            self.assertEqual(caught.exception.reason, "block_rejected", error)
            self.assertIn(error, caught.exception.message)

    def test_a_rejected_receive_or_open_is_still_a_node_error(self):
        # Only a send's record is freed by a rejection; a receive or an open
        # the node refuses answers as it did before: node_error.
        for subtype in ("receive", "open"):
            node, _ = self._node({"error": "Bad signature"})
            with self.assertRaises(nanonode.NodeError) as caught:
                node.process({"type": "state", "_subtype": subtype})
            self.assertEqual(caught.exception.reason, "node_error", subtype)
            self.assertIn("Bad signature", caught.exception.message)

    def test_an_answer_that_says_nothing_about_the_block_itself_is_not_a_rejection(self):
        # "Old block" means it IS on the ledger; "Fork" means it is in an
        # election it may still win; a gap means the node may apply it once the
        # block before it arrives; the RPC's subtype checks read the account's
        # CURRENT balance, so a block that landed earlier fails them once the
        # account has moved on; anything unrecognised is not a verdict.
        for error in ("Old block", "Fork", "Gap previous block", "Gap source block",
                      "Invalid block balance for given subtype", "Something new"):
            node, _ = self._node({"error": error})
            with self.assertRaises(nanonode.NodeError) as caught:
                node.process({"type": "state", "_subtype": "send"})
            self.assertEqual(caught.exception.reason, "node_error", error)

    def test_every_call_names_itself(self):
        # Cloudflare-fronted public nodes answer urllib's default
        # User-Agent with 403 "error code: 1010", indistinguishable from
        # a refused key.
        node, seen = self._node({"error": "Account not found"})
        node.account_info(NEW)
        agent = seen[0].get_header("User-agent") or ""
        self.assertTrue(agent.startswith("nano-wallet-xno/"), agent)



class WorkWhenTheNodeWillNotDoIt(unittest.TestCase):
    """A public node refuses `work_generate`. That used to end a receive."""

    def _node(self, answer, **kw):
        patch = mock.patch.object(urllib.request, "urlopen", _node_answering(answer, []))
        patch.start()
        self.addCleanup(patch.stop)
        return nanonode.HttpNanoNode("https://node.example/rpc", **kw)

    def _spy_on_solve(self):
        """Record what the node asks of `work.solve` and answer cheaply.

        The search itself is tested in `test_work.py`; what matters here is
        which threshold is asked for and whether it is asked for at all.
        """
        calls = []

        def solve(root, threshold=None, budget_seconds=None, _now=None):
            calls.append({"root": root, "threshold": threshold})
            return "0123456789abcdef"
        patch = mock.patch.object(nanonode._work, "solve", solve)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_a_refused_receive_falls_back_to_local_work(self):
        calls = self._spy_on_solve()
        node = self._node({"error": "Work generation is disabled"})
        self.assertEqual(node.work_generate("AB" * 32, "receive"), "0123456789abcdef")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["root"], bytes.fromhex("AB" * 32))

    def test_an_open_block_falls_back_too(self):
        self._spy_on_solve()
        node = self._node({"error": "Work generation is disabled"})
        self.assertEqual(node.work_generate("AB" * 32, "open"), "0123456789abcdef")

    def test_it_asks_only_for_the_receive_threshold(self):
        calls = self._spy_on_solve()
        node = self._node({"error": "Work generation is disabled"})
        node.work_generate("AB" * 32, "receive")
        self.assertEqual(calls[0]["threshold"], work.RECEIVE_THRESHOLD)
        self.assertLess(calls[0]["threshold"], work.SEND_THRESHOLD)

    def test_a_send_is_never_given_local_work(self):
        # At the send threshold this would take minutes. A send whose node
        # owes it work has to say so, not stall - and must never go out
        # under-worked at the receive threshold.
        calls = self._spy_on_solve()
        node = self._node({"error": "Work generation is disabled"})
        with self.assertRaises(nanonode.NodeError) as caught:
            node.work_generate("AB" * 32, "send")
        self.assertEqual(caught.exception.reason, "work_unavailable")
        self.assertEqual(calls, [])

    def test_an_unknown_subtype_is_not_given_local_work(self):
        calls = self._spy_on_solve()
        node = self._node({"error": "Work generation is disabled"})
        for subtype in (None, "", "change", "epoch"):
            with self.assertRaises(nanonode.NodeError):
                node.work_generate("AB" * 32, subtype)
        self.assertEqual(calls, [])

    def test_local_work_can_be_turned_off(self):
        calls = self._spy_on_solve()
        node = self._node({"error": "Work generation is disabled"}, local_work=False)
        with self.assertRaises(nanonode.NodeError) as caught:
            node.work_generate("AB" * 32, "receive")
        self.assertEqual(caught.exception.reason, "work_unavailable")
        self.assertEqual(calls, [])

    def test_a_node_that_does_the_work_is_still_preferred(self):
        calls = self._spy_on_solve()
        node = self._node({"work": "fedcba9876543210"})
        self.assertEqual(node.work_generate("AB" * 32, "receive"), "fedcba9876543210")
        self.assertEqual(calls, [], "the node answered; local work should not run")

    def test_a_node_answering_no_work_at_all_falls_back(self):
        self._spy_on_solve()
        node = self._node({"work": ""})
        self.assertEqual(node.work_generate("AB" * 32, "receive"), "0123456789abcdef")

    def test_an_unreachable_node_is_not_answered_with_a_minute_of_hashing(self):
        # The block could not be published either, so the caller needs the
        # real error rather than work it cannot use.
        calls = self._spy_on_solve()

        def refuse(request, timeout=None):
            raise urllib.error.URLError("no route to host")
        patch = mock.patch.object(urllib.request, "urlopen", refuse)
        patch.start()
        self.addCleanup(patch.stop)
        node = nanonode.HttpNanoNode("https://node.example/rpc")
        with self.assertRaises(nanonode.NodeError) as caught:
            node.work_generate("AB" * 32, "receive")
        self.assertEqual(caught.exception.reason, "node_unreachable")
        self.assertEqual(calls, [])

    def _node_refusing_with_http(self, status):
        def refuse(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, status, "Payment Required"
                                         if status == 402 else "refused", {}, io.BytesIO(b"{}"))
        patch = mock.patch.object(urllib.request, "urlopen", refuse)
        patch.start()
        self.addCleanup(patch.stop)
        return nanonode.HttpNanoNode("https://user:key@node.example/rpc")

    def test_a_node_refusing_with_http_402_falls_back_to_local_work(self):
        # rpc.nano.to - the node bounties.txt sends a new agent to - answers
        # `work_generate` with HTTP 402. The node was reached and said no, so
        # this is the refusal the fallback exists for, not an outage.
        calls = self._spy_on_solve()
        node = self._node_refusing_with_http(402)
        self.assertEqual(node.work_generate("AB" * 32, "receive"), "0123456789abcdef")
        self.assertEqual(len(calls), 1)

    def test_an_http_4xx_is_a_node_error_naming_the_status_not_the_credentials(self):
        node = self._node_refusing_with_http(402)
        with self.assertRaises(nanonode.NodeError) as caught:
            node.account_info(NEW)
        self.assertEqual(caught.exception.reason, "node_error")
        self.assertIn("402", str(caught.exception))
        self.assertNotIn("key", str(caught.exception))

    def test_an_http_5xx_is_still_unreachable_and_not_hashed_for(self):
        calls = self._spy_on_solve()
        node = self._node_refusing_with_http(503)
        with self.assertRaises(nanonode.NodeError) as caught:
            node.work_generate("AB" * 32, "receive")
        self.assertEqual(caught.exception.reason, "node_unreachable")
        self.assertEqual(calls, [])

    def test_a_failed_local_search_still_fails_closed(self):
        def give_up(root, threshold=None, budget_seconds=None, _now=None):
            raise work.WorkUnavailable("nothing found")
        patch = mock.patch.object(nanonode._work, "solve", give_up)
        patch.start()
        self.addCleanup(patch.stop)
        node = self._node({"error": "Work generation is disabled"})
        with self.assertRaises(nanonode.NodeError) as caught:
            node.work_generate("AB" * 32, "receive")
        self.assertEqual(caught.exception.reason, "work_unavailable")

    def test_a_bad_root_is_named_rather_than_hashed(self):
        self._spy_on_solve()
        node = self._node({"error": "Work generation is disabled"})
        with self.assertRaises(nanonode.NodeError) as caught:
            node.work_generate("nothex" * 10, "receive")
        self.assertEqual(caught.exception.reason, "bad_work_root")



def _at_an_easy_threshold(threshold):
    """`work.solve` pinned to an easier threshold, for a test that must be fast.

    `nanonode._work` IS the `work` module, so the real function has to be
    bound before the patch replaces it - otherwise the replacement calls
    itself.
    """
    real_solve = work.solve

    def solve(root, _threshold=None, budget_seconds=None, _now=None):
        return real_solve(root, threshold)
    return solve


class ANewWalletReceivesAgainstAPublicNode(unittest.TestCase):
    """The whole of issue #6, end to end, against one fake public node.

    The node answers the way rpc.nano.to does: `Account not found` for an
    account that has never received, and a refusal for `work_generate`.
    Before the two fixes this pair was fatal twice over. The work here is
    found by the real search, at an easier threshold so the test costs
    milliseconds - `test_work.py` pins the real threshold against a block
    the network accepted.
    """

    SOURCE = "B" * 64
    AMOUNT = "250000000000000000000000000000"
    EASY = 0x8000000000000000

    def setUp(self):
        self.published = []
        self.drained = False

        def urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            action = body["action"]
            if action == "account_info":
                return _Answer(json.dumps({"error": "Account not found"}).encode())
            if action == "receivable":
                if self.drained:
                    return _Answer(json.dumps({"blocks": {}}).encode())
                self.drained = True
                return _Answer(json.dumps(
                    {"blocks": {self.SOURCE: self.AMOUNT}}).encode())
            if action == "work_generate":
                return _Answer(json.dumps(
                    {"error": "Work generation is disabled"}).encode())
            if action == "process":
                self.published.append(body["block"])
                return _Answer(json.dumps({"hash": "C" * 64}).encode())
            raise AssertionError("unexpected call: " + action)

        for target, attr, value in (
            (urllib.request, "urlopen", urlopen),
            (nanonode._work, "solve", _at_an_easy_threshold(self.EASY)),
        ):
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_it_opens_its_account_and_the_work_is_valid(self):
        keys = _keystore.KeyStore()
        address = keys.put(bytes(range(1, 33)))
        node = nanonode.HttpNanoNode("https://node.example/rpc")

        result = payments.receive(address, node, keys)

        self.assertEqual(len(result["received"]), 1)
        self.assertEqual(result["balance_xno"], "0.250000")
        self.assertEqual(result["remaining_pending"], 0)

        self.assertEqual(len(self.published), 1)
        block = self.published[0]
        # An open block: no previous, and the link is the send it receives.
        self.assertEqual(block["previous"], "0" * 64)
        self.assertEqual(block["link"].upper(), self.SOURCE)
        self.assertEqual(block["balance"], self.AMOUNT)
        # The work is real work for this block's own root, which for an open
        # block is the account's public key.
        root = bytes.fromhex(_nanoaddr.decode(address).hex())
        self.assertTrue(work.validates(root, block["work"], self.EASY),
                        "the published block carries work that does not validate")
        self.assertEqual(len(block["work"]), 16)
        # No negative assertion here: at a lowered threshold half of all
        # (root, work) pairs validate by chance, so "it is not valid for
        # another root" would be a coin flip. That binding is pinned in
        # test_work.py, against the real threshold and a real block.

    def test_without_local_work_the_receive_still_fails_closed(self):
        keys = _keystore.KeyStore()
        address = keys.put(bytes(range(1, 33)))
        node = nanonode.HttpNanoNode("https://node.example/rpc", local_work=False)
        with self.assertRaises(payments.ToolError) as caught:
            payments.receive(address, node, keys)
        self.assertEqual(caught.exception.reason, "work_unavailable")
        self.assertEqual(self.published, [], "nothing may be published")


class ASendIsNeverBuiltOnAMixedAccountState(unittest.TestCase):
    """`account_info` took the frontier from the tip and the balance from the
    confirmation height, and `payments.send` subtracts the one from the other.

    `frontier`/`balance` describe the account's tip; `confirmed_frontier`/
    `confirmed_balance` describe it at its confirmation height. Two different
    points on the same chain, which differ for the second or two after the
    wallet publishes a block of its own - the ordinary state for an agent that
    was just paid and is paying on. Nano reads a send's amount as
    `previous.balance - block.balance`, so the gap between the two moments left
    the account on top of the payment: measured below, a 1 XNO send out of a
    tip holding 6 XNO against a confirmed 5 moved 2 XNO.

    Which of the two a send should be built on is a judgement about a chain the
    wallet is extending and is open in #4. These tests pin the narrower claim:
    while the two disagree, nothing is signed.
    """

    ONE = 10 ** 30
    TIP = "B" * 64
    CONFIRMED = "A" * 64
    REP = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"

    def _answers(self, info):
        self.published = []

        def urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            action = body["action"]
            if action == "account_info":
                return _Answer(json.dumps(info).encode())
            if action == "work_generate":
                return _Answer(json.dumps({"work": "0" * 16}).encode())
            if action == "process":
                self.published.append(body["block"])
                return _Answer(json.dumps({"hash": "C" * 64}).encode())
            raise AssertionError("unexpected call: " + action)

        patch = mock.patch.object(urllib.request, "urlopen", urlopen)
        patch.start()
        self.addCleanup(patch.stop)
        return nanonode.HttpNanoNode("https://node.example/rpc")

    def _info(self, **over):
        answer = {
            "frontier": self.TIP, "balance": str(6 * self.ONE),
            "confirmed_frontier": self.CONFIRMED,
            "confirmed_balance": str(5 * self.ONE),
            "representative": self.REP, "confirmed_representative": self.REP,
            "block_count": "7", "confirmed_height": "6",
        }
        answer.update(over)
        return answer

    def _send(self, node):
        keys = _keystore.KeyStore()
        address = keys.put(bytes(range(1, 33)))
        return payments.send(address, address, "1", "k1", node, keys, {},
                             env={"NANO_WALLET_ALLOW_SEND": "1"})

    # ---------------------------------------------------- what the node is asked

    def test_a_balance_from_another_block_is_reported_as_such(self):
        node = self._answers(self._info())
        self.assertFalse(node.account_info("nano_x")["balance_is_frontier_balance"])

    def test_a_settled_account_is_reported_as_settled(self):
        node = self._answers(self._info(confirmed_frontier=self.TIP))
        self.assertTrue(node.account_info("nano_x")["balance_is_frontier_balance"])

    def test_a_node_that_sends_no_confirmed_balance_is_already_one_point(self):
        # Then `balance_raw` is the tip's own balance, so the pair agrees.
        answer = self._info()
        del answer["confirmed_balance"]
        del answer["confirmed_frontier"]
        node = self._answers(answer)
        info = node.account_info("nano_x")
        self.assertTrue(info["balance_is_frontier_balance"])
        self.assertEqual(info["balance_raw"], 6 * self.ONE)

    def test_a_confirmed_balance_with_no_confirmed_frontier_is_not_proven(self):
        # The node has not said the two match, so it fails closed rather than
        # being given the benefit of the doubt on the send path.
        answer = self._info()
        del answer["confirmed_frontier"]
        node = self._answers(answer)
        self.assertFalse(node.account_info("nano_x")["balance_is_frontier_balance"])

    # --------------------------------------------------------------- the money

    def test_a_send_on_a_mixed_state_is_refused_and_nothing_is_signed(self):
        node = self._answers(self._info())
        with self.assertRaises(payments.ToolError) as caught:
            self._send(node)
        self.assertEqual(caught.exception.reason, "account_state_unsettled")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.published, [], "nothing may be published")

    def test_without_the_refusal_that_send_moves_twice_what_was_asked(self):
        # The defect itself, held in one place: build the same block from the
        # same answer with the guard's input flipped, and read the amount the
        # way Nano reads it.
        node = self._answers(self._info())
        real = node.account_info

        def mixed_but_unflagged(address):
            info = real(address)
            info["balance_is_frontier_balance"] = True
            return info

        node.account_info = mixed_but_unflagged
        self._send(node)
        self.assertEqual(len(self.published), 1)
        block = self.published[0]
        self.assertEqual(block["previous"], self.TIP)
        moved = 6 * self.ONE - int(block["balance"])
        self.assertEqual(
            moved, 2 * self.ONE,
            "a send of 1 XNO off this answer moves %d raw" % moved)

    def test_a_settled_account_still_sends_exactly_what_was_asked(self):
        node = self._answers(self._info(confirmed_frontier=self.TIP,
                                        confirmed_balance=str(6 * self.ONE)))
        result = self._send(node)
        self.assertEqual(result["amount_xno"], "1.000000")
        self.assertEqual(len(self.published), 1)
        block = self.published[0]
        self.assertEqual(block["previous"], self.TIP)
        self.assertEqual(6 * self.ONE - int(block["balance"]), self.ONE)


if __name__ == "__main__":
    unittest.main()
