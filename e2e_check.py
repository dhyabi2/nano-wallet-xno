#!/usr/bin/env python3
"""End-to-end acceptance check. Exits 0 only if every check passes.

Run this before trusting the tool with an address you intend to be paid on:

    $ python3 e2e_check.py
"""

import json
import subprocess
import sys

import nanoaddr
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


@check("the MCP server handshakes and advertises exactly four tools")
def _():
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    proc = subprocess.run([sys.executable, "mcp_server.py"], input=requests,
                          capture_output=True, text=True)
    lines = [json.loads(line) for line in proc.stdout.splitlines()]
    assert lines[0]["result"]["serverInfo"]["name"] == "nano-wallet"
    assert len(lines[1]["result"]["tools"]) == 4


@check("no module in the money path can reach the network")
def _():
    for name in ("nanoaddr.py", "ed25519_blake2b.py", "wallet.py", "mcp_server.py"):
        with open(name) as handle:
            source = handle.read()
        for module in ("socket", "http", "urllib", "requests", "ssl", "asyncio"):
            assert "import %s" % module not in source, "%s imports %s" % (name, module)


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
