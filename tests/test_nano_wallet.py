"""Tests for the Nano wallet tool.

The known-answer vectors below are the point of this file. Two of them are
public facts about the Nano network that nothing in this repository can
influence, so they prove the implementation rather than restate it:

  * the all-zero public key encodes to the canonical burn address
  * the mainnet genesis public key encodes to the genesis address
  * RFC 8032 Ed25519 vector 1 pins the curve arithmetic, with SHA-512
  * the all-zero seed pins Nano's BLAKE2b variant of that same arithmetic
"""

import importlib.util
import io
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
import ed25519_blake2b as ed
import mcp_server
import nanoaddr
import wallet

BURN = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
GENESIS_PK = "E89208DD038FBB269987689621D52292AE9C35941A7484756ECCED92A65093BA"
GENESIS = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
ZERO_SEED_ACCOUNT = "nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7"
ZERO_SEED_PRIV = "9F0E444C69F77A49BD0BE89DB92C38FE713E0963165CCA12FAF5712D7657120F"
ZERO_SEED_PUB = "C008B814A7D269A1FA3C6528B19201A24D797912DB9996FF02A1FF356E45552B"


class TestAddressCodec(unittest.TestCase):
    def test_burn_address_vector(self):
        self.assertEqual(nanoaddr.encode(bytes(32)), BURN)

    def test_genesis_address_vector(self):
        self.assertEqual(nanoaddr.encode(bytes.fromhex(GENESIS_PK)), GENESIS)

    def test_decode_is_the_inverse_of_encode(self):
        self.assertEqual(nanoaddr.decode(GENESIS).hex().upper(), GENESIS_PK)

    def test_round_trip_over_many_keys(self):
        for i in range(64):
            key = bytes([i]) * 32
            self.assertEqual(nanoaddr.decode(nanoaddr.encode(key)), key)

    def test_legacy_xrb_prefix_is_accepted_and_normalised(self):
        legacy = "xrb_" + GENESIS[len("nano_"):]
        verdict = nanoaddr.validate(legacy)
        self.assertTrue(verdict["valid"])
        self.assertEqual(verdict["prefix"], "xrb_")
        self.assertEqual(verdict["normalised"], GENESIS)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertTrue(nanoaddr.is_valid("  %s\n" % GENESIS))


class TestRejection(unittest.TestCase):
    """The eddie_researcher case: an address that looks right and is not.

    Each of these fails loudly WITH A REASON. Without checksum validation
    every one of them is accepted as a payout destination.
    """

    def assertRejected(self, address, reason):
        verdict = nanoaddr.validate(address)
        self.assertFalse(verdict["valid"], "%r was accepted" % address)
        self.assertEqual(verdict["reason"], reason)
        self.assertTrue(verdict["message"])

    def test_one_flipped_checksum_character_is_caught(self):
        broken = GENESIS[:-1] + ("1" if GENESIS[-1] != "1" else "3")
        self.assertRejected(broken, "bad_checksum")

    def test_every_single_character_substitution_in_the_body_is_caught(self):
        # A transposition or typo anywhere in the address must not survive.
        for position in range(len("nano_"), len(GENESIS)):
            original = GENESIS[position]
            replacement = "4" if original != "4" else "5"
            if position == len("nano_") and replacement not in "13":
                replacement = "1" if original == "3" else "3"
            candidate = GENESIS[:position] + replacement + GENESIS[position + 1:]
            self.assertFalse(
                nanoaddr.is_valid(candidate),
                "a single-character change at %d survived validation" % position,
            )

    def test_truncated_address(self):
        self.assertRejected(GENESIS[:-1], "bad_length")

    def test_missing_prefix(self):
        self.assertRejected(GENESIS[len("nano_"):], "bad_prefix")

    def test_wrong_prefix(self):
        self.assertRejected("nano-" + GENESIS[len("nano_"):], "bad_prefix")

    def test_characters_outside_the_nano_alphabet(self):
        # 0, 2, l and v are not in Nano's base32 alphabet.
        for bad in "02lv":
            candidate = GENESIS[:10] + bad + GENESIS[11:]
            self.assertRejected(candidate, "bad_character")

    def test_bad_padding_bits(self):
        self.assertRejected("nano_4" + GENESIS[len("nano_") + 1:], "bad_padding")

    def test_empty_and_non_string(self):
        self.assertRejected("", "empty")
        self.assertRejected("   ", "empty")
        self.assertRejected(None, "not_a_string")

    def test_an_ethereum_address_is_not_a_nano_address(self):
        self.assertRejected("0x71C7656EC7ab88b098defB751B7401B5f6d8976F", "bad_prefix")

    def test_decode_raises_with_a_reason_attribute(self):
        with self.assertRaises(nanoaddr.InvalidAddress) as caught:
            nanoaddr.decode(GENESIS[:-1])
        self.assertEqual(caught.exception.reason, "bad_length")


class TestKeyDerivation(unittest.TestCase):
    def test_rfc8032_vector_pins_the_curve_arithmetic(self):
        secret = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        )
        public = ed.public_key_from_private(secret, ed._sha512)
        self.assertEqual(
            public.hex(),
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        )

    def test_rfc8032_signature_vector(self):
        secret = bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
        )
        public = ed.public_key_from_private(secret, ed._sha512)
        signature = ed.sign(b"", secret, public, ed._sha512)
        self.assertEqual(
            signature.hex(),
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
            "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        )

    def test_zero_seed_vector_pins_the_blake2b_variant(self):
        private = ed.private_key_from_seed(bytes(32), 0)
        self.assertEqual(private.hex().upper(), ZERO_SEED_PRIV)
        public = ed.public_key_from_private(private)
        self.assertEqual(public.hex().upper(), ZERO_SEED_PUB)
        self.assertEqual(nanoaddr.encode(public), ZERO_SEED_ACCOUNT)

    def test_indices_give_different_accounts(self):
        seed = bytes(32)
        addresses = {
            nanoaddr.encode(ed.public_key_from_private(ed.private_key_from_seed(seed, i)))
            for i in range(8)
        }
        self.assertEqual(len(addresses), 8)

    def test_seed_and_index_are_validated(self):
        with self.assertRaises(ValueError):
            ed.private_key_from_seed(b"short", 0)
        with self.assertRaises(ValueError):
            ed.private_key_from_seed(bytes(32), -1)
        with self.assertRaises(ValueError):
            ed.private_key_from_seed(bytes(32), 2 ** 32)

    def test_generated_seed_is_32_bytes_and_not_constant(self):
        self.assertEqual(len(ed.generate_seed()), 32)
        self.assertNotEqual(ed.generate_seed(), ed.generate_seed())


class TestWallet(unittest.TestCase):
    def test_create_wallet_emits_a_checksum_valid_address(self):
        for _ in range(16):
            account = wallet.create_wallet()
            self.assertTrue(
                nanoaddr.is_valid(account["address"]),
                "create_wallet emitted an address that fails its own checksum",
            )
            self.assertEqual(len(account["seed"]), 64)
            self.assertEqual(len(account["private_key"]), 64)

    def test_derive_account_matches_the_zero_seed_vector(self):
        account = wallet.derive_account("00" * 32, 0)
        self.assertEqual(account["address"], ZERO_SEED_ACCOUNT)

    def test_derive_account_rejects_a_bad_seed(self):
        for bad in ("", "zz" * 32, "00" * 16):
            with self.assertRaises(ValueError):
                wallet.derive_account(bad, 0)

    def test_sign_message_recovers_the_signing_address(self):
        account = wallet.derive_account("00" * 32, 3)
        signed = wallet.sign_message(b"proof of control", account["private_key"])
        self.assertEqual(signed["address"], account["address"])
        self.assertEqual(len(signed["signature"]), 128)

    def test_nothing_in_the_money_path_imports_a_network_module(self):
        """The custody claim, stated as a test rather than a promise.

        Checked TRANSITIVELY. 1.0.0 grepped each file for `import urllib`,
        which stopped being enough the moment 1.1.0 added a node client:
        a module that imports a module that imports urllib reaches the
        network just as well as one that imports it directly.
        """
        import ast
        forbidden = {"socket", "http", "urllib", "requests", "ssl", "asyncio", "ftplib"}
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def imports_of(module):
            with open(os.path.join(here, module + ".py")) as handle:
                tree = ast.parse(handle.read())
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    found.add(node.module.split(".")[0])
            return found

        def reachable(start):
            seen, todo = set(), [start]
            while todo:
                name = todo.pop()
                if name in seen:
                    continue
                seen.add(name)
                if os.path.exists(os.path.join(here, name + ".py")):
                    todo.extend(imports_of(name))
            return seen

        # The offline core. `mcp_server` is deliberately NOT here: balance and
        # receive need a node, and it reaches one through `nanonode`. What holds
        # for it instead is the assertion below.
        for name in ("nanoaddr", "ed25519_blake2b", "wallet", "blocks", "keystore",
                     "profiles", "capabilities"):
            leaks = sorted(reachable(name) & forbidden)
            self.assertEqual(leaks, [], "%s can reach %s" % (name, leaks))

        # And exactly one module in the wallet opens a connection to a node.
        # e2e_* are the acceptance harnesses, not part of the installed package.
        network_capable = sorted(
            name[:-3] for name in os.listdir(here)
            if name.endswith(".py") and not name.startswith("e2e_")
            and imports_of(name[:-3]) & forbidden
        )
        self.assertEqual(
            network_capable, ["nanonode", "selfcheck", "wellknown"],
            "the set of network-touching modules changed: %s. nanonode speaks to the "
            "node, wellknown serves one read-only document, and selfcheck imports "
            "socket only to BLOCK it." % network_capable,
        )


class TestAmounts(unittest.TestCase):
    def test_exact_conversion_both_ways(self):
        for text in ("0", "1", "0.000001", "0.000000000000000000000000000001", "133248.297"):
            self.assertEqual(wallet.raw_to_xno(wallet.xno_to_raw(text)), text)

    def test_one_raw_is_the_smallest_unit(self):
        self.assertEqual(wallet.xno_to_raw("0.000000000000000000000000000001"), 1)

    def test_sub_cent_amounts_do_not_round_to_zero(self):
        self.assertEqual(wallet.xno_to_raw("0.0000005"), 5 * 10 ** 23)

    def test_rejects_nonsense(self):
        for bad in ("", "-1", "abc", "1.2.3", "0." + "1" * 31):
            with self.assertRaises(ValueError):
                wallet.xno_to_raw(bad)
        with self.assertRaises(ValueError):
            wallet.raw_to_xno(-1)
        with self.assertRaises(ValueError):
            wallet.raw_to_xno("1")


class TestMcpServer(unittest.TestCase):
    def roundtrip(self, *requests):
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
        stdout = io.StringIO()
        mcp_server.serve(stdin, stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_initialize_and_list_tools(self):
        responses = self.roundtrip(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "nano-wallet")
        names = [t["name"] for t in responses[1]["result"]["tools"]]
        # 1.1.0 added profiles (specs/agent-tool-receive-only-onboarding.md). The
        # default profile now also registers the spec-named tools and the gated
        # `send`; the four names 1.0.0 shipped are still here, unchanged and in
        # the same relative order, so a client written against 1.0.0 is unaffected.
        self.assertEqual(
            names,
            ["create_address", "validate_address", "balance", "receive", "send",
             "nano_validate_address", "nano_create_wallet",
             "nano_derive_account", "nano_sign_message"],
        )
        self.assertEqual(responses[0]["result"]["serverInfo"]["profile"], "full")

    def test_every_advertised_tool_is_callable(self):
        for tool in mcp_server.TOOLS:
            self.assertIn("inputSchema", tool)
            self.assertTrue(tool["description"].strip())

    def test_validate_tool_reports_a_bad_checksum_without_erroring(self):
        broken = GENESIS[:-1] + "1"
        responses = self.roundtrip({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "nano_validate_address", "arguments": {"address": broken}},
        })
        payload = responses[0]["result"]["structuredContent"]
        self.assertFalse(payload["valid"])
        self.assertEqual(payload["reason"], "bad_checksum")
        self.assertFalse(responses[0]["result"]["isError"])

    def test_create_wallet_over_mcp(self):
        responses = self.roundtrip({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "nano_create_wallet", "arguments": {}},
        })
        self.assertTrue(nanoaddr.is_valid(responses[0]["result"]["structuredContent"]["address"]))

    def test_bad_arguments_are_an_error_result_not_a_crash(self):
        responses = self.roundtrip({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "nano_derive_account", "arguments": {"seed": "nope"}},
        })
        self.assertTrue(responses[0]["result"]["isError"])

    def test_unknown_tool_and_unknown_method(self):
        responses = self.roundtrip(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "nano_send_everything", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 7, "method": "wallet/drain"},
        )
        # A tool that is not registered is -32601 method not found, the same answer
        # as a method that was never defined. 1.0.0 answered -32602 here; the
        # receive-only profile turns "this tool is absent" into a load-bearing
        # statement, and -32602 (invalid params) would imply the tool exists.
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[1]["error"]["code"], -32601)

    def test_notifications_get_no_response(self):
        self.assertEqual(self.roundtrip({"jsonrpc": "2.0", "method": "notifications/initialized"}), [])

    def test_malformed_line_is_a_parse_error_not_a_crash(self):
        stdin = io.StringIO("{not json\n")
        stdout = io.StringIO()
        mcp_server.serve(stdin, stdout)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], -32700)


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            code = cli.main(list(args))
        finally:
            sys.stdout = saved
        return code, buffer.getvalue()

    def test_check_exits_zero_for_a_valid_address(self):
        code, output = self.run_cli("check", GENESIS)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["valid"])

    def test_check_exits_nonzero_for_a_bad_checksum(self):
        code, output = self.run_cli("check", GENESIS[:-1] + "1")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["reason"], "bad_checksum")

    def test_new_and_derive(self):
        code, output = self.run_cli("new")
        self.assertEqual(code, 0)
        self.assertTrue(nanoaddr.is_valid(json.loads(output)["address"]))
        code, output = self.run_cli("derive", "00" * 32, "0")
        self.assertEqual(json.loads(output)["address"], ZERO_SEED_ACCOUNT)

    def test_amount_commands(self):
        _, output = self.run_cli("raw", "0.000001")
        self.assertEqual(json.loads(output)["raw"], "1000000000000000000000000")
        _, output = self.run_cli("xno", "1000000000000000000000000")
        self.assertEqual(json.loads(output)["xno"], "0.000001")

    def test_usage_errors_exit_two(self):
        self.assertEqual(self.run_cli("check")[0], 2)
        self.assertEqual(self.run_cli("nonsense")[0], 2)
        self.assertEqual(self.run_cli()[0], 0)



class TestHelpNamesSomethingThatExists(unittest.TestCase):
    """`--help` is the first thing a new install prints. It must not name a
    command that does not exist.

    It did: the first line read `python3 -m nano_wallet <command>`, and there is
    no `nano_wallet` module in this package or in the wheel it builds -- the
    modules are installed flat (`cli`, `wallet`, ...) and the entry point users
    actually get is the `nano-wallet` console script from `[project.scripts]`.
    A reader who copied that first line got "No module named nano_wallet".
    """

    def help_text(self):
        buffer = io.StringIO()
        stdout, sys.stdout = sys.stdout, buffer
        try:
            self.assertEqual(cli.main(["--help"]), 0)
        finally:
            sys.stdout = stdout
        return buffer.getvalue()

    def test_every_module_the_help_offers_is_importable(self):
        offered = re.findall(r"python3?\s+-m\s+([A-Za-z_][A-Za-z0-9_.]*)",
                             self.help_text())
        for module in offered:
            with self.subTest(module=module):
                self.assertIsNotNone(
                    importlib.util.find_spec(module),
                    "--help offers `python3 -m %s`, which is not an importable "
                    "module, so the reader gets 'No module named %s'"
                    % (module, module))

    def test_the_help_names_the_console_script_that_runs_this_module(self):
        """The invocation that reaches *this* help, read out of pyproject.toml.

        Deliberately only the script whose target is `cli:main`. The project
        installs others -- `mandate` is its own command with its own entry
        point -- and cli.py's help has no business naming them; an earlier
        version of this law demanded every script appear here and went red the
        moment a second one was added, which was the law being wrong, not the
        help. Reading the name rather than restating it still turns this red if
        the script is renamed without updating the help.

        Parsed with a regex, not tomllib, because this package supports 3.8.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            pyproject = fh.read()
        scripts = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)",
                            pyproject, re.S)
        self.assertIsNotNone(scripts, "pyproject.toml has no [project.scripts]")
        mine = re.findall(r"""^\s*([A-Za-z0-9_.-]+)\s*=\s*['"]cli:main['"]""",
                          scripts.group(1), re.M)
        self.assertTrue(
            mine, "[project.scripts] has no entry pointing at cli:main, so the "
                  "command that prints this help can no longer be named")
        first_line = self.help_text().strip().splitlines()[0]
        for name in mine:
            with self.subTest(script=name):
                self.assertIn(name, first_line)

if __name__ == "__main__":
    unittest.main()
