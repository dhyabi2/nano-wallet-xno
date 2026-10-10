#!/usr/bin/env python3
"""End-to-end acceptance check. Exits 0 only if every check passes.

Run this before trusting the tool with an address you intend to be paid on:

    $ python3 e2e_check.py
"""

import json
import os
import subprocess
import sys

import nanoaddr
import profiles
import wallet

BURN = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
GENESIS = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
GENESIS_PK = "E89208DD038FBB269987689621D52292AE9C35941A7484756ECCED92A65093BA"
ZERO_SEED_ACCOUNT = "nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7"

CHECKS = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn
    return register


@check("all-zero public key encodes to the canonical burn address")
def _():
    assert nanoaddr.encode(bytes(32)) == BURN


@check("mainnet genesis public key encodes to the genesis address")
def _():
    assert nanoaddr.encode(bytes.fromhex(GENESIS_PK)) == GENESIS


@check("the all-zero seed reproduces Nano's published first account")
def _():
    assert wallet.derive_account("00" * 32, 0)["address"] == ZERO_SEED_ACCOUNT


@check("a valid address validates, and reports its public key")
def _():
    verdict = wallet.validate(GENESIS)
    assert verdict["valid"] and verdict["public_key"] == GENESIS_PK


@check("a single flipped character is rejected as bad_checksum")
def _():
    verdict = wallet.validate(GENESIS[:-1] + "1")
    assert verdict["valid"] is False and verdict["reason"] == "bad_checksum"


@check("every single-character change anywhere in the address is rejected")
def _():
    for position in range(len("nano_"), len(GENESIS)):
        original = GENESIS[position]
        replacement = "4" if original != "4" else "5"
        if position == len("nano_"):
            replacement = "1" if original == "3" else "3"
        candidate = GENESIS[:position] + replacement + GENESIS[position + 1:]
        assert not nanoaddr.is_valid(candidate), "survived at %d" % position


@check("a truncated address is rejected as bad_length")
def _():
    assert wallet.validate(GENESIS[:-1])["reason"] == "bad_length"


@check("an address with no prefix is rejected as bad_prefix")
def _():
    assert wallet.validate(GENESIS[5:])["reason"] == "bad_prefix"


@check("a freshly created wallet always passes its own checksum")
def _():
    for _unused in range(25):
        assert nanoaddr.is_valid(wallet.create_wallet()["address"])


@check("signing proves control of the address without revealing the key")
def _():
    account = wallet.derive_account("00" * 32, 7)
    proof = wallet.sign_message(b"unstuck", account["private_key"])
    assert proof["address"] == account["address"]
    assert len(proof["signature"]) == 128


@check("sub-cent amounts convert exactly, with no float in the path")
def _():
    assert wallet.xno_to_raw("0.0000005") == 5 * 10 ** 23
    assert wallet.raw_to_xno(1) == "0.000000000000000000000000000001"


@check("the CLI exits 0 on a valid address and 1 on a bad checksum")
def _():
    ok = subprocess.run([sys.executable, "cli.py", "check", GENESIS], capture_output=True)
    bad = subprocess.run([sys.executable, "cli.py", "check", GENESIS[:-1] + "1"], capture_output=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)["valid"] is True
    assert bad.returncode == 1 and json.loads(bad.stdout)["reason"] == "bad_checksum"


@check("the MCP server handshakes and advertises exactly its profile's tools")
def _():
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    proc = subprocess.run([sys.executable, "mcp_server.py"], input=requests,
                          capture_output=True, text=True)
    lines = [json.loads(line) for line in proc.stdout.splitlines()]
    assert lines[0]["result"]["serverInfo"]["name"] == "nano-wallet"
    names = [tool["name"] for tool in lines[1]["result"]["tools"]]
    assert names == list(profiles.tools_for(profiles.DEFAULT_PROFILE)), names


# The root tests/test_work.py reads off the mainnet, and work for it that
# clears the SEND threshold - the one no free public node will do. Found here by
# `work.solve_parallel` on four cores in 137 seconds.
WORK_ROOT = "8789CF4B88407CB006F5A53F6FEEECFCDC6E56F8AE51B9BBF806317A33643490"
SEND_WORK = "0ba27e49039735a0"


@check("a send's proof-of-work is findable without a node, and the vector proves it")
def _():
    import work
    assert work.validates(bytes.fromhex(WORK_ROOT), SEND_WORK, work.SEND_THRESHOLD)
    # The same root's network-accepted receive work does NOT clear it, so this
    # is the two thresholds differing and not the two roots differing.
    assert not work.validates(bytes.fromhex(WORK_ROOT), "58fc8abfa35fcfee",
                              work.SEND_THRESHOLD)


@check("`work estimate` says what a send costs on THIS machine, measured")
def _():
    run = subprocess.run([sys.executable, "cli.py", "work", "estimate",
                          "--seconds", "0.1"], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    said = json.loads(run.stdout)
    assert said["hashes_per_second"] > 0, said
    assert said["send"]["expected_hashes"] == 2 ** 29, said
    assert said["receive"]["expected_hashes"] == 2 ** 23, said
    assert said["send"]["average_seconds"] > said["receive"]["average_seconds"]


@check("`work precompute` stores verified work the next block can be sent with")
def _():
    import tempfile

    import prework
    import work
    with tempfile.TemporaryDirectory() as cache:
        run = subprocess.run([sys.executable, "cli.py", "work", "precompute",
                              WORK_ROOT, "--threshold", "receive",
                              "--cache", cache, "--budget", "300"],
                             capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
        said = json.loads(run.stdout)
        assert said["precomputed"] == WORK_ROOT, said
        # Read back by a SECOND process, through the same reader the send path
        # uses, and verified here rather than taken from the report.
        held = prework.WorkCache(cache).get(WORK_ROOT, work.RECEIVE_THRESHOLD)
        assert held == said["work"], (held, said)
        assert work.validates(bytes.fromhex(WORK_ROOT), held, work.RECEIVE_THRESHOLD)
        listed = subprocess.run([sys.executable, "cli.py", "work", "pending",
                                 "--cache", cache], capture_output=True, text=True)
        assert json.loads(listed.stdout)["have_work_for"] == [WORK_ROOT], listed.stdout


@check("the work cache is off, not defaulted, when nothing configures it")
def _():
    import prework
    assert prework.WorkCache.from_env({}) is None
    bare = {k: v for k, v in os.environ.items() if k != prework.CACHE_ENV}
    run = subprocess.run([sys.executable, "cli.py", "work", "pending"],
                         capture_output=True, text=True, env=bare)
    assert run.returncode == 2, run.stdout
    assert json.loads(run.stdout)["error"] == "no_work_cache", run.stdout


#: Every module that must be unable to reach the network, checked TRANSITIVELY:
#: it is not enough that they do not import urllib themselves, because an import
#: two hops away reaches the network just as well.
OFFLINE_MODULES = ("nanoaddr", "ed25519_blake2b", "wallet", "blocks", "keystore",
                   "profiles", "capabilities", "work", "prework")

NETWORK_MODULES = ("socket", "http", "urllib", "requests", "ssl", "asyncio", "ftplib")


def _imports_of(module_name):
    import ast
    with open(module_name + ".py") as handle:
        tree = ast.parse(handle.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _reachable(start):
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if os.path.exists(name + ".py"):
            todo.extend(_imports_of(name))
    return seen


@check("no module in the money path can reach the network, transitively")
def _():
    for name in OFFLINE_MODULES:
        reachable = _reachable(name)
        leaks = sorted(reachable & set(NETWORK_MODULES))
        assert not leaks, "%s can reach %s" % (name, ", ".join(leaks))


#: nanonode.py speaks to the node; wellknown.py serves one read-only document.
#: selfcheck.py imports socket only in order to BLOCK it - `no_sockets()` replaces
#: socket.socket with something that raises, which is how check 5 proves that
#: address creation made no network call rather than asserting it did not.
EXEMPT = {"nanonode.py", "wellknown.py", "selfcheck.py",
          "e2e_check.py", "e2e_receive_only.py", "fakenode.py"}


@check("nanonode.py is the ONLY module in the wallet that touches the network")
def _():
    for name in os.listdir("."):
        if not name.endswith(".py") or name in EXEMPT:
            continue
        direct = _imports_of(name[:-3]) & set(NETWORK_MODULES)
        assert not direct, "%s imports %s directly" % (name, ", ".join(sorted(direct)))


def main():
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:
            failures += 1
            print("FAIL  %s\n      %s" % (name, exc))
        else:
            print("ok    %s" % name)
    print("\n%d/%d checks passed" % (len(CHECKS) - failures, len(CHECKS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
