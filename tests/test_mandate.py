"""Operator mandate laws for nano-wallet: the vendored module agrees with its
source, and `send` obeys an operator-signed cap before anything is broadcast.

Offline: the node is fakenode.FakeNode. Keys are obviously synthetic.
Expected values come from outside the code under test: a canonical JSON
string written by hand and hashed with hashlib, and a signature produced by
the independent C library ed25519-blake2b 1.4.1.
"""

import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fakenode
import keystore
import mandate as M
import mcp_server
import payments
import profiles

# Bump only together with agent-wallet-multirail/src/agent_wallet_multirail/mandate.py
# and openai-agents-nano-x402/src/openai_agents_nano/mandate.py: the three copies are one file.
VENDORED_SHA256 = "80d7a441dd8df9a1bb51e1e8556032792c4f2a9203e81a684adf6593d1148612"

OPERATOR_SEED = "11" * 32  # test-fixture
OPERATOR_KEY = M.private_key_from_seed(bytes.fromhex(OPERATOR_SEED), 0)
OPERATOR = "nano_1bwjtpipkzc7aj6hmuodncjmfsb4tou9word8bj9jxcm68cheipad54q66xe"
AGENT = "nano_3166w1xhtgg1b46izokwhxq9aist9wagha6eouuce7fy5fyrbb1xjgzwwm3c"
PAYEE = "nano_1p7cqqnfo91zwcnse54f3ucyfi6xwzsbnkt3ay14cd1g6epmiygnt7jtcoa5"
BURN = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
RAW = 10 ** 30

CANONICAL = (
    '{"agent":"nano_3166w1xhtgg1b46izokwhxq9aist9wagha6eouuce7fy5fyrbb1xjgzwwm3c",'
    '"allowed_payees":["nano_1p7cqqnfo91zwcnse54f3ucyfi6xwzsbnkt3ay14cd1g6epmiygnt7jtcoa5"],'
    '"expires_at":"2030-01-01T00:00:00Z","issued_at":"2026-09-27T00:00:00Z",'
    '"nonce":"00112233445566778899aabbccddeeff",'
    '"operator":"nano_1bwjtpipkzc7aj6hmuodncjmfsb4tou9word8bj9jxcm68cheipad54q66xe",'
    '"per_payment_max_raw":"10000000000000000000000000000",'
    '"purpose":"Buy web-search API calls for the research task",'
    '"total_cap_raw":"500000000000000000000000000000",'
    '"type":"nano-operator-mandate","version":1}'
)
GOLDEN_HASH = "3967D88A777758AF459E77D8C81DCFDA2E91A6841A58BD6FA7CFC5DBFFE1505F"
GOLDEN_SIGNATURE = (
    "19A401D0CFD28D8EAB5D126EE314246021E40BEF107C7995A8515A86175D916F"
    "531EDE2C43C75444851AC82663D8E391FD1BFB7392E11AE5FF291FA8DA53220E"
)


class VendoredModuleAgrees(unittest.TestCase):

    def test_the_vendored_file_is_the_pinned_bytes(self):
        with open(os.path.join(ROOT, "mandate.py"), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), VENDORED_SHA256)

    def test_golden_canonical_hash_and_signature(self):
        mandate = M.build_mandate(
            AGENT, OPERATOR, str(RAW // 2), str(RAW // 100),
            "Buy web-search API calls for the research task", "2030-01-01T00:00:00Z",
            allowed_payees=[PAYEE], issued_at="2026-09-27T00:00:00Z",
            nonce="00112233445566778899aabbccddeeff")
        self.assertEqual(M.canonical_bytes(mandate).decode(), CANONICAL)
        expected = hashlib.blake2b(b"nano-operator-mandate/v1\n" + CANONICAL.encode(), digest_size=32)
        self.assertEqual(expected.hexdigest().upper(), GOLDEN_HASH)
        signed = M.sign_mandate(mandate, OPERATOR_KEY)
        self.assertEqual((signed["hash"], signed["signature"]), (GOLDEN_HASH, GOLDEN_SIGNATURE))
        M.verify_signed(signed, now=dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc))

    def test_zero_seed_vector(self):
        public = M.public_key_from_private(M.private_key_from_seed(bytes(32), 0))
        self.assertEqual(M.address_from_public_key(public),
                         "nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7")


class SendUnderAMandate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.keys, self.node = keystore.KeyStore(), fakenode.FakeNode()
        self.agent = payments.create_address(store="memory", keys=self.keys)["address"]
        self.node.fund(self.agent, 10 * RAW)
        self.path = os.path.join(self.tmp, "mandate.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, agent=None, total="0.3", per="0.1", payees=(BURN,), issued=None, expires="2030-01-01T00:00:00Z",
              tamper=None):
        mandate = M.build_mandate(agent or self.agent, OPERATOR, M.xno_to_raw(total), M.xno_to_raw(per),
                                  "Pay the burn address in tests", expires, allowed_payees=list(payees) or None,
                                  issued_at=issued)
        signed = M.sign_mandate(mandate, OPERATOR_KEY)
        if tamper:
            tamper(signed)
        with open(self.path, "w") as fh:
            json.dump(signed, fh)

    def server(self, **extra):
        env = {"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_MANDATE": self.path}
        env.update(extra)
        server = mcp_server.Server(profiles.FULL, env=env, node=self.node, keys=self.keys)
        server.call_tool("receive", {"address": self.agent})
        return server

    def send(self, server, amount, key, to=BURN):
        return server.call_tool("send", {"from": self.agent, "to": to, "amount_xno": amount,
                                         "idempotency_key": key})

    def refused(self, server, amount, key, to=BURN):
        before = len(self.node.published)
        with self.assertRaises(payments.ToolError) as caught:
            self.send(server, amount, key, to)
        self.assertEqual(caught.exception.reason, "mandate_refused")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(self.node.published), before, "a refused send must broadcast nothing")
        return caught.exception.extra["mandate_reason"]

    def test_within_the_mandate_sends_and_records(self):
        self.write()
        server = self.server()
        self.send(server, "0.1", "a")
        status = M.MandateGuard.from_file(self.path, agent=self.agent).status()
        self.assertEqual(int(status["spent_raw"]), RAW // 10)
        self.assertEqual(int(status["remaining_raw"]), 2 * RAW // 10)

    def test_cap_exhaustion(self):
        self.write()
        server = self.server()
        for key in "abc":
            self.send(server, "0.1", key)
        self.assertEqual(self.refused(server, "0.000001", "d"), "cap_exhausted")

    def test_replay_is_not_counted_twice(self):
        self.write()
        server = self.server()
        self.send(server, "0.1", "a")
        self.send(server, "0.1", "a")
        status = M.MandateGuard.from_file(self.path, agent=self.agent).status()
        self.assertEqual(int(status["spent_raw"]), RAW // 10)

    def test_per_payment_max(self):
        self.write()
        self.assertEqual(self.refused(self.server(), "0.1000001", "a"), "over_per_payment_max")

    def test_wrong_payee(self):
        self.write(payees=(PAYEE,))
        self.assertEqual(self.refused(self.server(), "0.01", "a"), "payee_not_allowed")

    def test_expired(self):
        self.write(issued="2026-01-01T00:00:00Z", expires="2026-01-02T00:00:00Z")
        self.assertEqual(self.refused(self.server(), "0.01", "a"), "expired")

    def test_a_mandate_for_another_agent(self):
        self.write(agent=AGENT)
        self.assertEqual(self.refused(self.server(), "0.01", "a"), "wrong_agent")

    def test_tampered_mandate(self):
        def raise_cap(signed):
            signed["mandate"]["total_cap_raw"] = str(100 * RAW)
            signed["hash"] = M.mandate_hash(signed["mandate"])
        self.write(tamper=raise_cap)
        self.assertEqual(self.refused(self.server(), "0.01", "a"), "bad_signature")

    def test_missing_mandate_file_refuses_instead_of_sending_uncapped(self):
        self.assertEqual(self.refused(self.server(), "0.01", "a"), "unreadable_mandate")

    def test_require_mandate_without_one(self):
        server = mcp_server.Server(profiles.FULL, node=self.node, keys=self.keys,
                                   env={"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_REQUIRE_MANDATE": "1"})
        server.call_tool("receive", {"address": self.agent})
        with self.assertRaises(payments.ToolError) as caught:
            self.send(server, "0.01", "a")
        self.assertEqual(caught.exception.reason, "mandate_required")

    def test_a_failed_broadcast_stays_counted(self):
        self.write()
        server = self.server()
        real = self.node.process

        def lost(block):
            raise __import__("nanonode").NodeError("node_unreachable", "timed out")

        self.node.process = lost
        with self.assertRaises(payments.ToolError):
            self.send(server, "0.1", "a")
        self.node.process = real
        status = M.MandateGuard.from_file(self.path, agent=self.agent).status()
        self.assertEqual(int(status["spent_raw"]), RAW // 10)


if __name__ == "__main__":
    unittest.main()
