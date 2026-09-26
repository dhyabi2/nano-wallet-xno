"""Tests for the receive-only profile: specs/agent-tool-receive-only-onboarding.md.

The ten numbered tests from that spec are here under their spec names, in
spec order, followed by the error table and the money paths. Nothing here
opens a socket: the node is `fakenode.FakeNode`, and the offline checks
run under `selfcheck.no_sockets()`, which makes any socket construction
raise rather than merely asserting that none happened.
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blocks
import capabilities
import cli
import fakenode
import keystore
import mcp_server
import nanonode
import payments
import profiles
import selfcheck
import wallet
import wellknown

BURN = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
GENESIS = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
RAW = 10 ** 30


def a_new_address(keys):
    """A fresh address whose key is loaded in `keys`."""
    return payments.create_address(store="memory", keys=keys)["address"]


def call(server, name, arguments=None):
    """One MCP tools/call round trip, as a client would make it."""
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})


# ------------------------------------------------------ the ten spec tests

class SpecTests(unittest.TestCase):

    def test_send_tool_absent_from_tools_list(self):
        """1. tools/list under receive-only is exactly the four tools."""
        server = mcp_server.Server(profiles.RECEIVE_ONLY, env={})
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, {"create_address", "validate_address", "balance", "receive"})

    def test_send_call_is_method_not_found(self):
        """2. calling send is -32601, not a permission error."""
        server = mcp_server.Server(profiles.RECEIVE_ONLY, env={})
        response = call(server, "send", {"from": BURN, "to": BURN, "amount_xno": "1",
                                         "idempotency_key": "k"})
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)
        self.assertNotIn("403", response["error"]["message"])

    def test_profile_cannot_be_widened_by_env(self):
        """3. receive-only + NANO_WALLET_ALLOW_SEND=1 exits 2 and serves nothing."""
        env = {"NANO_WALLET_ALLOW_SEND": "1"}
        with self.assertRaises(profiles.ProfileError) as caught:
            mcp_server.Server(profiles.RECEIVE_ONLY, env=env)
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(caught.exception.message, profiles.WIDENING_MESSAGE)

        # ...and the process really does not go on to serve. main() returns 2
        # without reading a single request from stdin.
        stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
        stdout, stderr = io.StringIO(), io.StringIO()
        real_in, real_out, real_err = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
        os.environ["NANO_WALLET_ALLOW_SEND"] = "1"
        try:
            code = mcp_server.main(["--profile", "receive-only"])
        finally:
            sys.stdin, sys.stdout, sys.stderr = real_in, real_out, real_err
            os.environ.pop("NANO_WALLET_ALLOW_SEND", None)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stdin.tell(), 0)       # stdin was never read

    def test_selfcheck_passes_offline(self):
        """4. every socket blocked; selfcheck exits 0 with 7/7."""
        out = io.StringIO()
        with selfcheck.no_sockets():
            code = selfcheck.main(["--profile", "receive-only"], env={}, out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("7/7 pass", out.getvalue())
        self.assertIn("this install can receive XNO and cannot spend it", out.getvalue())

    def test_selfcheck_fails_if_send_present(self):
        """5. an install configured for the full profile is NOT receive-only."""
        out = io.StringIO()
        code = selfcheck.main(["--profile", "receive-only"],
                              env={"NANO_WALLET_PROFILE": profiles.FULL}, out=out)
        self.assertEqual(code, 1)
        self.assertIn("NOT receive-only", out.getvalue())

    def test_selfcheck_json_shape(self):
        """6. --json parses, pass is a bool, 7 checks, each with name and pass."""
        out = io.StringIO()
        selfcheck.main(["--profile", "receive-only", "--json"], env={}, out=out)
        verdict = json.loads(out.getvalue())
        self.assertIsInstance(verdict["pass"], bool)
        self.assertEqual(len(verdict["checks"]), selfcheck.CHECK_COUNT)
        self.assertEqual(len(verdict["checks"]), 7)
        for check in verdict["checks"]:
            self.assertIn("name", check)
            self.assertIsInstance(check["pass"], bool)

    def test_capabilities_document_is_exact(self):
        """7. the document is asserted against the LIVE server, not a constant."""
        document = capabilities.document(profiles.RECEIVE_ONLY)
        self.assertEqual(document["tools_absent"], ["send"])

        server = mcp_server.Server(profiles.RECEIVE_ONLY, env={})
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        live = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(document["tools_registered"], live)

        sentence = document["operator_scope_sentence"]
        self.assertIn("cannot send", sentence)
        self.assertTrue(sentence.endswith("."))
        self.assertIn("Nano (XNO)", sentence)               # names the asset
        self.assertIn("accept incoming payments", sentence) # states the capability
        # Fixed in code, not templated: the same object twice is the same string.
        self.assertEqual(sentence, capabilities.OPERATOR_SCOPE_SENTENCE)
        self.assertEqual(capabilities.document(profiles.RECEIVE_ONLY)["operator_scope_sentence"],
                         sentence)

    def test_receive_works_in_profile(self):
        """8. one pending block is pocketed and the balance reflects it, with
        sending unavailable throughout."""
        keys = keystore.KeyStore()
        node = fakenode.FakeNode()
        server = mcp_server.Server(profiles.RECEIVE_ONLY, env={}, node=node, keys=keys)
        address = a_new_address(keys)
        node.fund(address, 5 * RAW // 100)                  # 0.05 XNO

        received = server.call_tool("receive", {"address": address})
        self.assertEqual(len(received["received"]), 1)
        self.assertEqual(received["received"][0]["amount_xno"], "0.050000")
        self.assertEqual(received["balance_xno"], "0.050000")
        self.assertEqual(received["remaining_pending"], 0)

        after = server.call_tool("balance", {"address": address})
        self.assertEqual(after["balance_xno"], "0.050000")
        self.assertEqual(after["pending_xno"], "0.000000")
        self.assertNotIn("send", server.tool_names())

    def test_no_key_in_any_profile_output(self):
        """9. no seed or private key in any output of any registered tool."""
        keys = keystore.KeyStore()
        node = fakenode.FakeNode()
        server = mcp_server.Server(profiles.RECEIVE_ONLY, env={}, node=node, keys=keys)

        created = server.call_tool("create_address", {"label": "payouts"})
        address = created["address"]
        private_key = keys.get(address).hex()
        node.fund(address, RAW)

        blob = json.dumps(created)
        for name, arguments in (("validate_address", {"address": address}),
                                ("balance", {"address": address}),
                                ("receive", {"address": address})):
            blob += json.dumps(server.call_tool(name, arguments))
        blob += json.dumps(selfcheck.run(profiles.RECEIVE_ONLY, env={}))
        blob += capabilities.as_json(profiles.RECEIVE_ONLY)

        self.assertNotIn(private_key, blob.lower())
        self.assertNotIn("private_key", blob)
        self.assertNotIn('"seed"', blob)
        # The word "seed" DOES appear, once, in the capability document's list of
        # what this install cannot do. That is the opposite of a leak.
        self.assertIn("private key or seed", capabilities.as_json(profiles.RECEIVE_ONLY))

    def test_wellknown_document_served(self):
        """10. the served document and the CLI's are one source of truth."""
        server = wellknown.make_server("127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d%s" % (port, capabilities.WELL_KNOWN_PATH)
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "application/json")
                served = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        printed = json.loads(capabilities.as_json(profiles.RECEIVE_ONLY))
        for field in ("can", "cannot", "tools_registered", "tools_absent"):
            self.assertEqual(served[field], printed[field], field)
        self.assertEqual(served, printed)


# ------------------------------------------------------------ error table

class ErrorTable(unittest.TestCase):

    def test_unknown_profile_exits_2_and_lists_the_known_ones(self):
        out = io.StringIO()
        code = selfcheck.main(["--profile", "wide-open"], env={}, out=out)
        self.assertEqual(code, 2)
        for name in profiles.PROFILE_NAMES:
            self.assertIn(name, out.getvalue())

    def test_capabilities_unknown_profile_exits_2(self):
        with self.assertRaises(capabilities.UnknownProfile):
            capabilities.document("wide-open")

    def test_selfcheck_aborts_rather_than_print_a_leak(self):
        """If check 6 ever fails, nothing further is printed."""
        verdict = selfcheck.run(profiles.RECEIVE_ONLY, env={})
        broken = dict(verdict, checks=[
            dict(check, **({"pass": False} if check["name"].startswith("private key") else {}))
            for check in verdict["checks"]])
        out = io.StringIO()
        real_run = selfcheck.run
        selfcheck.run = lambda *a, **k: broken
        try:
            code = selfcheck.main(["--profile", "receive-only"], env={}, out=out)
        finally:
            selfcheck.run = real_run
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "selfcheck aborted: key material found in its own output\n")

    def test_bad_address_makes_no_network_call(self):
        node = fakenode.FakeNode()
        with self.assertRaises(payments.ToolError) as caught:
            payments.balance(BURN[:-1] + "x", node)
        self.assertEqual(caught.exception.reason, "invalid_address")
        self.assertEqual(node.calls, [])

    def test_receive_without_the_key_is_key_not_loaded(self):
        node = fakenode.FakeNode()
        with self.assertRaises(payments.ToolError) as caught:
            payments.receive(BURN, node, keystore.KeyStore())
        self.assertEqual(caught.exception.reason, "key_not_loaded")

    def test_receive_rejects_max_blocks_out_of_range(self):
        keys = keystore.KeyStore()
        address = a_new_address(keys)
        for bad in (0, 65, "many"):
            with self.assertRaises(payments.ToolError) as caught:
                payments.receive(address, fakenode.FakeNode(), keys, bad)
            self.assertEqual(caught.exception.reason, "invalid_max_blocks")

    def test_node_unreachable_is_503_and_names_only_the_host(self):
        node = fakenode.FakeNode(
            fail_with=nanonode.NodeError("node_unreachable",
                                         "could not reach the Nano node at node.example: timed out"))
        keys = keystore.KeyStore()
        address = a_new_address(keys)
        with self.assertRaises(payments.ToolError) as caught:
            payments.balance(address, node)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("node.example", caught.exception.message)

    def test_node_url_credentials_never_appear_in_a_message(self):
        self.assertEqual(nanonode.host_of("https://user:secret@node.example:7076/rpc"),
                         "node.example:7076")
        self.assertNotIn("secret", nanonode.host_of("https://user:secret@node.example/"))

    def test_key_file_is_0600_and_never_overwritten(self):
        directory = tempfile.mkdtemp()
        try:
            path = os.path.join(directory, "payouts.key")
            keys = keystore.KeyStore()
            created = payments.create_address(label="payouts", store="file", path=path, keys=keys)
            self.assertEqual(created["stored"], "file:%s" % path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertNotIn("private_key", created)

            with self.assertRaises(payments.ToolError) as caught:
                payments.create_address(store="file", path=path, keys=keys)
            self.assertEqual(caught.exception.reason, "key_exists")
        finally:
            shutil.rmtree(directory)

    def test_create_address_rejects_a_bad_store_and_a_long_label(self):
        for arguments, reason in (({"store": "s3"}, "invalid_store"),
                                  ({"label": "x" * 65}, "invalid_label"),
                                  ({"store": "file"}, "invalid_path")):
            with self.assertRaises(payments.ToolError) as caught:
                payments.create_address(**arguments)
            self.assertEqual(caught.exception.reason, reason)

    def test_validate_says_what_a_payment_would_do(self):
        verdict = payments.validate_address(BURN[:-1] + "3")
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["reason"], "invalid_checksum")
        self.assertIn("can never arrive", verdict["message"])

    def test_xrb_prefix_is_normalised(self):
        verdict = payments.validate_address("xrb_" + BURN[len("nano_"):])
        self.assertTrue(verdict["valid"])
        self.assertEqual(verdict["prefix_normalised_from"], "xrb_")
        self.assertEqual(verdict["canonical"], BURN)

    def test_wellknown_rejects_other_paths_and_methods(self):
        server = wellknown.make_server("127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request("http://127.0.0.1:%d/secrets" % port)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 404)

            post = urllib.request.Request("http://127.0.0.1:%d%s" % (port, capabilities.WELL_KNOWN_PATH),
                                          data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(post, timeout=5)
            self.assertIn(caught.exception.code, (405, 501))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# --------------------------------------------------------------- the money

class Money(unittest.TestCase):
    """Nothing below may be computed with a float. 1 XNO = 10**30 raw, and a
    double holds 53 bits of mantissa, so a single float round trip loses real
    money in the low-order digits."""

    def test_one_raw_round_trips(self):
        self.assertEqual(wallet.xno_to_raw(wallet.raw_to_xno(1)), 1)
        self.assertEqual(wallet.raw_to_xno(1), "0." + "0" * 29 + "1")
        # _six PADS to six places, it does not truncate to them: one raw keeps
        # all thirty of its digits. Truncating here is how a balance quietly
        # loses money, so assert the long form rather than the pretty one.
        self.assertEqual(payments._six(1), "0." + "0" * 29 + "1")
        self.assertEqual(payments._six(0), "0.000000")
        self.assertEqual(payments._six(10 ** 30), "1.000000")

    def test_receive_of_one_raw_is_exact(self):
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        address = a_new_address(keys)
        node.fund(address, 1)
        payments.receive(address, node, keys)
        self.assertEqual(node.accounts[address]["balance_raw"], 1)

    def test_two_receives_add_exactly(self):
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        address = a_new_address(keys)
        node.fund(address, RAW // 10)                       # 0.1
        node.fund(address, RAW // 5)                        # 0.2
        result = payments.receive(address, node, keys)
        self.assertEqual(len(result["received"]), 2)
        self.assertEqual(result["balance_xno"], "0.300000")
        self.assertEqual(node.accounts[address]["balance_raw"], 3 * RAW // 10)

    def test_receive_chains_blocks_the_node_accepts(self):
        """The second block's `previous` is the first block's hash, and the fake
        node rejects a fork - so this passing means the chain is really valid."""
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        address = a_new_address(keys)
        node.fund(address, RAW)
        node.fund(address, RAW)
        payments.receive(address, node, keys)
        self.assertEqual(len(node.published), 2)
        self.assertEqual(node.published[0]["previous"], "0" * 64)
        self.assertEqual(node.published[1]["previous"], fakenode._hash_of(node.published[0]))

    def test_block_signature_verifies_against_the_account(self):
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        address = a_new_address(keys)
        node.fund(address, RAW)
        payments.receive(address, node, keys)
        import ed25519_blake2b as ed
        import nanoaddr
        block = node.published[0]
        public_key = nanoaddr.decode(block["account"])
        digest = bytes.fromhex(fakenode._hash_of(block))
        private_key = keys.get(address)
        self.assertEqual(ed.sign(digest, private_key, public_key).hex().upper(),
                         block["signature"])

    def test_a_float_balance_is_refused(self):
        with self.assertRaises(ValueError):
            blocks.block_hash(bytes(32), bytes(32), bytes(32), 1.0, bytes(32))
        with self.assertRaises(ValueError):
            wallet.raw_to_xno(1.0)

    def test_first_block_is_self_represented(self):
        """No third party is chosen on the agent's behalf."""
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        address = a_new_address(keys)
        node.fund(address, RAW)
        payments.receive(address, node, keys)
        self.assertEqual(node.published[0]["representative"], address)


# -------------------------------------------------- the other profile only

class SendIsGated(unittest.TestCase):
    """`send` exists only in the full profile, and is refused there by default.
    Its presence is what makes test 5 above meaningful."""

    def test_send_is_registered_in_the_full_profile(self):
        server = mcp_server.Server(profiles.FULL, env={})
        self.assertIn("send", server.tool_names())

    def test_send_is_refused_without_explicit_authority(self):
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        server = mcp_server.Server(profiles.FULL, env={}, node=node, keys=keys)
        address = a_new_address(keys)
        with self.assertRaises(payments.ToolError) as caught:
            server.call_tool("send", {"from": address, "to": BURN,
                                      "amount_xno": "0.1", "idempotency_key": "k1"})
        self.assertEqual(caught.exception.reason, "spend_not_enabled")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(node.calls, [])

    def test_send_is_idempotent_and_conflicts_are_refused(self):
        env = {"NANO_WALLET_ALLOW_SEND": "1"}
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        server = mcp_server.Server(profiles.FULL, env=env, node=node, keys=keys)
        address = a_new_address(keys)
        node.fund(address, RAW)
        server.call_tool("receive", {"address": address})

        first = server.call_tool("send", {"from": address, "to": BURN,
                                          "amount_xno": "0.1", "idempotency_key": "k1"})
        published = len(node.published)
        again = server.call_tool("send", {"from": address, "to": BURN,
                                          "amount_xno": "0.1", "idempotency_key": "k1"})
        self.assertEqual(again["block_hash"], first["block_hash"])
        self.assertEqual(len(node.published), published)     # nothing sent twice

        with self.assertRaises(payments.ToolError) as caught:
            server.call_tool("send", {"from": address, "to": GENESIS,
                                      "amount_xno": "0.1", "idempotency_key": "k1"})
        self.assertEqual(caught.exception.reason, "idempotency_conflict")
        self.assertEqual(caught.exception.status, 409)

    def test_send_respects_the_per_call_limit(self):
        env = {"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_MAX_SEND_XNO": "0.5"}
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        server = mcp_server.Server(profiles.FULL, env=env, node=node, keys=keys)
        address = a_new_address(keys)
        node.fund(address, 10 * RAW)
        server.call_tool("receive", {"address": address})
        with self.assertRaises(payments.ToolError) as caught:
            server.call_tool("send", {"from": address, "to": BURN,
                                      "amount_xno": "0.6", "idempotency_key": "k2"})
        self.assertEqual(caught.exception.reason, "send_limit_exceeded")

    def test_send_refuses_more_than_the_balance(self):
        env = {"NANO_WALLET_ALLOW_SEND": "1"}
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        server = mcp_server.Server(profiles.FULL, env=env, node=node, keys=keys)
        address = a_new_address(keys)
        node.fund(address, RAW // 10)
        server.call_tool("receive", {"address": address})
        with self.assertRaises(payments.ToolError) as caught:
            server.call_tool("send", {"from": address, "to": BURN,
                                      "amount_xno": "0.2", "idempotency_key": "k3"})
        self.assertEqual(caught.exception.reason, "insufficient_balance")
        self.assertEqual(caught.exception.extra["available_xno"], "0.100000")

    def test_send_moves_exactly_the_amount(self):
        env = {"NANO_WALLET_ALLOW_SEND": "1"}
        keys, node = keystore.KeyStore(), fakenode.FakeNode()
        server = mcp_server.Server(profiles.FULL, env=env, node=node, keys=keys)
        address = a_new_address(keys)
        node.fund(address, RAW)
        server.call_tool("receive", {"address": address})
        server.call_tool("send", {"from": address, "to": BURN,
                                  "amount_xno": "0.000001", "idempotency_key": "k4"})
        self.assertEqual(node.accounts[address]["balance_raw"], RAW - RAW // 10 ** 6)


class CliSurface(unittest.TestCase):

    def test_capabilities_command_prints_the_document(self):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            code = cli.main(["capabilities", "--profile", "receive-only"])
        finally:
            sys.stdout = real
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()),
                         capabilities.document(profiles.RECEIVE_ONLY))

    def test_capabilities_unknown_profile_exits_2(self):
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            code = cli.main(["capabilities", "--profile", "wide-open"])
        finally:
            sys.stdout = real
        self.assertEqual(code, 2)
        self.assertIn("unknown_profile", out.getvalue())


if __name__ == "__main__":
    unittest.main()
