#!/usr/bin/env python3
"""End-to-end acceptance for the receive-only profile. Exits 0 only if all pass.

    $ python3 e2e_receive_only.py

This drives the REAL entry points - the MCP server as a subprocess over
stdio, the CLI as a subprocess, and the well-known document over a real
loopback HTTP request - rather than calling the functions behind them. A
test that imports the server and a client that spawns it are not the
same thing, and the spec's claim is about what an operator sees when
they run the command.

The one network socket this file opens is to 127.0.0.1, for the
well-known endpoint, which is the endpoint under test. Nothing here
reaches a Nano node: the node is `fakenode.FakeNode`.
"""

import json
import os
import subprocess
import sys
import threading
import urllib.request

import capabilities
import fakenode
import keystore
import mcp_server
import payments
import profiles
import selfcheck
import wellknown

RAW = 10 ** 30
CHECKS = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn
    return register


def run_server(args, requests, env=None):
    """Spawn mcp_server.py for real and speak JSON-RPC to its stdin."""
    environment = dict(os.environ)
    environment.pop("NANO_WALLET_ALLOW_SEND", None)
    environment.pop("NANO_WALLET_PROFILE", None)
    environment.update(env or {})
    proc = subprocess.run(
        [sys.executable, "mcp_server.py"] + args,
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True, text=True, env=environment, timeout=60,
    )
    lines = [json.loads(line) for line in proc.stdout.splitlines()]
    return proc, lines


def run_cli(args, env=None):
    environment = dict(os.environ)
    environment.pop("NANO_WALLET_PROFILE", None)
    environment.update(env or {})
    return subprocess.run([sys.executable, "cli.py"] + args,
                          capture_output=True, text=True, env=environment, timeout=60)


# --- spec test 1 ---------------------------------------------------------

@check("1. a real receive-only server advertises exactly four tools, send absent")
def _():
    proc, lines = run_server(
        ["--profile", "receive-only"],
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
    )
    names = [tool["name"] for tool in lines[0]["result"]["tools"]]
    assert names == ["create_address", "validate_address", "balance", "receive"], names
    assert "send" not in names


# --- spec test 2 ---------------------------------------------------------

@check("2. calling send on it is -32601 method not found, not a 403")
def _():
    proc, lines = run_server(
        ["--profile", "receive-only"],
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "send", "arguments": {"from": "x", "to": "y",
                                                   "amount_xno": "1", "idempotency_key": "k"}}}],
    )
    assert lines[0]["error"]["code"] == -32601, lines[0]
    assert "403" not in json.dumps(lines[0])
    assert "spend_not_enabled" not in json.dumps(lines[0])


# --- spec test 3 ---------------------------------------------------------

@check("3. the profile cannot be widened by an env var: exit 2, nothing served")
def _():
    proc, lines = run_server(
        ["--profile", "receive-only"],
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
        env={"NANO_WALLET_ALLOW_SEND": "1"},
    )
    assert proc.returncode == 2, proc.returncode
    assert lines == [], lines
    assert "cannot be combined with send authority" in proc.stderr


# --- spec tests 4, 5, 6 --------------------------------------------------

@check("4. selfcheck exits 0 with 7/7 through the real CLI, no node configured")
def _():
    result = run_cli(["selfcheck", "--profile", "receive-only"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "7/7 pass" in result.stdout
    assert result.stdout.rstrip().endswith("cannot spend it.")
    assert "[fail]" not in result.stdout


@check("5. selfcheck on a full-profile install exits 1 and says NOT receive-only")
def _():
    result = run_cli(["selfcheck", "--profile", "receive-only"],
                     env={"NANO_WALLET_PROFILE": "full"})
    assert result.returncode == 1, result.returncode
    assert "NOT receive-only" in result.stdout


@check("6. selfcheck --json parses, pass is a bool, exactly 7 named checks")
def _():
    result = run_cli(["selfcheck", "--profile", "receive-only", "--json"])
    verdict = json.loads(result.stdout)
    assert isinstance(verdict["pass"], bool)
    assert len(verdict["checks"]) == 7, len(verdict["checks"])
    for entry in verdict["checks"]:
        assert entry["name"] and isinstance(entry["pass"], bool)


# --- spec test 7 ---------------------------------------------------------

@check("7. the capability document matches the LIVE tool list, not a constant")
def _():
    document = json.loads(run_cli(["capabilities", "--profile", "receive-only"]).stdout)
    _, lines = run_server(["--profile", "receive-only"],
                          [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    live = [tool["name"] for tool in lines[0]["result"]["tools"]]
    assert document["tools_registered"] == live, (document["tools_registered"], live)
    assert document["tools_absent"] == ["send"]
    sentence = document["operator_scope_sentence"]
    assert "cannot send" in sentence and sentence.endswith(".")


# --- spec test 8 ---------------------------------------------------------

@check("8. receive pockets a pending block and the balance reflects it")
def _():
    keys, node = keystore.KeyStore(), fakenode.FakeNode()
    server = mcp_server.Server(profiles.RECEIVE_ONLY, env={}, node=node, keys=keys)
    address = server.call_tool("create_address", {"label": "payouts"})["address"]
    node.fund(address, 5 * RAW // 100)

    result = server.call_tool("receive", {"address": address})
    assert result["received"][0]["amount_xno"] == "0.050000", result
    assert result["remaining_pending"] == 0
    assert server.call_tool("balance", {"address": address})["balance_xno"] == "0.050000"
    assert "send" not in server.tool_names()


# --- spec test 9 ---------------------------------------------------------

@check("9. no key material in any output of any tool, over the real subprocess")
def _():
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "create_address", "arguments": {"label": "payouts"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    proc, lines = run_server(["--profile", "receive-only"], requests)
    blob = proc.stdout + proc.stderr
    assert '"seed"' not in blob
    assert '"private_key"' not in blob
    # And the created address is real, so the response is not empty of substance.
    created = lines[0]["result"]["structuredContent"]
    assert payments.validate_address(created["address"])["valid"] is True
    assert created["stored"] == "memory (lost on restart)"


# --- spec test 10 --------------------------------------------------------

@check("10. GET /.well-known/nano-receive-only == the CLI's document, byte for byte")
def _():
    server = wellknown.make_server("127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d%s" % (port, capabilities.WELL_KNOWN_PATH)
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "application/json"
            body = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    printed = run_cli(["capabilities", "--profile", "receive-only"]).stdout
    assert json.loads(body) == json.loads(printed)
    for field in ("can", "cannot", "tools_registered", "tools_absent"):
        assert json.loads(body)[field] == json.loads(printed)[field], field


# --- the onboarding sequence itself --------------------------------------

@check("the four-step onboarding runs in order and step 3 yields a valid address")
def _():
    # 1. start the server under the profile
    proc, lines = run_server(["--profile", "receive-only"],
                             [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
    assert lines[0]["result"]["serverInfo"]["profile"] == "receive-only"
    # 2. selfcheck, and read the last line to the operator
    result = run_cli(["selfcheck", "--profile", "receive-only"])
    last = [line for line in result.stdout.splitlines() if line.strip()][-1]
    assert last.startswith("selfcheck: 7/7 pass")
    # 3. create_address, and validate what came back
    _, created = run_server(["--profile", "receive-only"],
                            [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "create_address", "arguments": {}}}])
    address = created[0]["result"]["structuredContent"]["address"]
    assert payments.validate_address(address)["valid"] is True
    # 4. is the work queue's job, not this package's - it is the reason for 1-3.


@check("the scope sentence an operator approves is fixed, not templated")
def _():
    first = run_cli(["capabilities", "--profile", "receive-only"]).stdout
    second = run_cli(["capabilities", "--profile", "receive-only"]).stdout
    assert first == second
    assert json.loads(first)["operator_scope_sentence"] == capabilities.OPERATOR_SCOPE_SENTENCE


# --- the error table -----------------------------------------------------

@check("error table: an unknown profile exits 2 and lists the known ones")
def _():
    result = run_cli(["selfcheck", "--profile", "wide-open"])
    assert result.returncode == 2, result.returncode
    for name in profiles.PROFILE_NAMES:
        assert name in result.stdout
    capabilities_result = run_cli(["capabilities", "--profile", "wide-open"])
    assert capabilities_result.returncode == 2


@check("error table: a bad address makes no network call at all")
def _():
    node = fakenode.FakeNode()
    try:
        payments.balance("nano_" + "1" * 59 + "x", node)
    except payments.ToolError as exc:
        assert exc.reason == "invalid_address", exc.reason
    else:
        raise AssertionError("a bad address was accepted")
    assert node.calls == [], node.calls


@check("error table: receive without the key loaded is key_not_loaded")
def _():
    keys, node = keystore.KeyStore(), fakenode.FakeNode()
    stray = payments.create_address(store="memory")["address"]   # key kept elsewhere
    try:
        payments.receive(stray, node, keys)
    except payments.ToolError as exc:
        assert exc.reason == "key_not_loaded", exc.reason
    else:
        raise AssertionError("received into an account whose key is not loaded")


@check("the selfcheck's offline guard really blocks sockets")
def _():
    import socket
    with selfcheck.no_sockets():
        try:
            socket.socket()
        except selfcheck.NetworkAttempted:
            pass
        else:
            raise AssertionError("no_sockets() let a socket through")
    socket.socket().close()            # and puts it back afterwards


def main():
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:
            failures += 1
            print("FAIL  %s\n      %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("ok    %s" % name)
    print("\n%d/%d receive-only end-to-end checks passed" % (len(CHECKS) - failures, len(CHECKS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
