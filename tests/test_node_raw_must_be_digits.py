"""A raw amount the node reports is read as digits, or not at all.

`HttpNanoNode.account_info` read the account's balance with a bare `int()`, and
`receivable` read each pending amount the same way. `int()` reads more than a
node sends:

  * a JSON number. Raw carries 31 digits where a float64 holds about 16, so a
    balance of 6900000000000000000000000000000 raw arriving as the number
    6.9e30 comes back as 6899999999999999883876432347136 - 116123567652864 raw
    low.
  * `true`, as 1.
  * a non-ASCII digit string ("٣" is 3), which `str.isdigit()` also allows,
    which is why the check below is `isdigit() and isascii()` - the same pair
    `wallet.xno_to_raw` already uses on the agent's own amount.

The balance is not a number this wallet only reports. `payments.send` builds the
block's new balance as `balance_raw - amount_raw`, while Nano reads the amount a
send moved as the LEDGER's `previous.balance - block.balance`. The rounding is
therefore not cancelled out by being used on both sides: measured below, against
a node that answers the balance above as the number 6.9e30, an asked 1 XNO signs
a block whose balance is 5899999999999999883876432347136 - so the ledger reads
1.000000000000000116123567652864 XNO moved, 116123567652864 raw more than the
agent asked for. A value that rounds the other way pays the seller short
instead.

The fix is a refusal, not a conversion: a reply that does not carry raw as ASCII
digits is not a usable answer about the account, so it is a `node_error` - the
caller holds and retries, and nothing is signed or published.
"""

import io
import json
import os
import sys
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keystore as _keystore  # noqa: E402
import nanonode  # noqa: E402
import payments  # noqa: E402

ONE = 10 ** 30
REP = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
TIP = "B" * 64


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ARawAmountFromTheNodeMustBeAsciiDigits(unittest.TestCase):
    def _node(self, info=None, receivable=None):
        """A node answering `account_info` with `info` and `receivable` with
        `receivable`. Every published block is captured, never sent."""
        self.published = []

        def urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            action = body["action"]
            if action == "account_info":
                return _Answer(json.dumps(info or {}).encode())
            if action == "receivable":
                return _Answer(json.dumps(receivable or {"blocks": ""}).encode())
            if action == "work_generate":
                return _Answer(json.dumps({"work": "0" * 16}).encode())
            if action == "process":
                self.published.append(body["block"])
                return _Answer(json.dumps({"hash": "C" * 64}).encode())
            raise AssertionError("unexpected call: " + action)

        patch = mock.patch.object(urllib.request, "urlopen", urlopen)
        patch.start()
        self.addCleanup(patch.stop)
        return nanonode.HttpNanoNode("https://user:key@node.example/rpc")

    def _info(self, balance):
        return {"frontier": TIP, "balance": balance, "representative": REP,
                "confirmed_frontier": TIP, "confirmed_balance": balance,
                "block_count": "7", "confirmed_height": "7"}

    def _send(self, node, amount="1"):
        keys = _keystore.KeyStore()
        address = keys.put(bytes(range(1, 33)))
        return payments.send(address, address, amount, "k1", node, keys, {},
                             env={"NANO_WALLET_ALLOW_SEND": "1"})

    # -------------------------------------------------- the balance on account_info

    def test_a_balance_the_node_sends_as_digits_is_read(self):
        node = self._node(self._info(str(6 * ONE)))
        self.assertEqual(node.account_info("nano_x")["balance_raw"], 6 * ONE)

    def test_a_balance_that_is_a_json_number_is_refused(self):
        # 6.9e30 raw cannot survive a float64: int() of it is 116123567652864
        # raw below the balance the ledger holds.
        node = self._node(self._info(6.9e30))
        with self.assertRaises(nanonode.NodeError) as caught:
            node.account_info("nano_x")
        self.assertEqual(caught.exception.reason, "node_error")
        self.assertIn("balance", caught.exception.message)
        self.assertNotIn("key", caught.exception.message, "never the node's credentials")

    def test_a_balance_that_is_an_integral_json_number_is_refused_too(self):
        # It is not lossless either - 10**30 is exact as a float, 10**30 + 1 is
        # not - and a node does not send raw as a number at all.
        node = self._node(self._info(6 * 10 ** 30))
        with self.assertRaises(nanonode.NodeError):
            node.account_info("nano_x")

    def test_a_boolean_balance_is_refused_rather_than_read_as_one_raw(self):
        node = self._node(self._info(True))
        with self.assertRaises(nanonode.NodeError):
            node.account_info("nano_x")

    def test_a_non_ascii_digit_balance_is_refused(self):
        node = self._node(self._info("٣"))  # ARABIC-INDIC THREE, int() reads 3
        with self.assertRaises(nanonode.NodeError):
            node.account_info("nano_x")

    def test_a_balance_that_is_not_a_number_at_all_is_refused(self):
        for balance in ("", "   ", "-1", "1.9", "0x10", "1_000", None, [], {}):
            node = self._node(self._info(balance))
            with self.assertRaises(nanonode.NodeError, msg=repr(balance)):
                node.account_info("nano_x")

    # ----------------------------------------------------------- what a send signs

    def test_a_send_against_a_json_number_balance_signs_nothing(self):
        # The defect, measured on this exact answer before the fix: the block is
        # signed and published with balance 5899999999999999883876432347136, so
        # the ledger (whose previous.balance is the real 6.9 XNO) reads
        # 1.000000000000000116123567652864 XNO moved for an asked 1.
        node = self._node(self._info(6.9e30))
        with self.assertRaises(payments.ToolError) as caught:
            self._send(node)
        self.assertEqual(caught.exception.reason, "node_error")
        self.assertEqual(self.published, [], "nothing may be signed or published")

    def test_a_send_against_a_digit_balance_still_moves_exactly_what_was_asked(self):
        node = self._node(self._info(str(6 * ONE)))
        self._send(node)
        self.assertEqual(len(self.published), 1)
        self.assertEqual(int(self.published[0]["balance"]), 5 * ONE,
                         "6 XNO minus the 1 XNO asked for")

    # -------------------------------------------------------- receivable amounts

    def test_a_receivable_amount_that_is_a_json_number_is_refused(self):
        node = self._node(receivable={"blocks": {"A" * 64: {"amount": 1.9e30,
                                                            "source": "nano_y"}}})
        with self.assertRaises(nanonode.NodeError) as caught:
            node.receivable("nano_x", 10)
        self.assertEqual(caught.exception.reason, "node_error")

    def test_a_bare_receivable_amount_is_read_when_it_is_digits_and_refused_when_not(self):
        # The node answers `{"<hash>": "<amount>"}` without `source=true`.
        node = self._node(receivable={"blocks": {"A" * 64: str(2 * ONE)}})
        self.assertEqual(node.receivable("nano_x", 10),
                         [{"hash": "A" * 64, "amount_raw": 2 * ONE, "source": None}])
        node = self._node(receivable={"blocks": {"A" * 64: 2.0e30}})
        with self.assertRaises(nanonode.NodeError):
            node.receivable("nano_x", 10)


if __name__ == "__main__":
    unittest.main()
