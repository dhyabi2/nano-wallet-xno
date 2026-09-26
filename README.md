# nano-wallet — a Nano (XNO) wallet as one install

Four operations, no dependencies, no network, no account:

    create address · derive account · validate address (checksum) · prove control

Python 3.8+ and the standard library. Nothing else. `pip install` is not
required and there is no package to resolve — copy the directory, or clone it.

## Why this exists

The swarm wrote to 501 outside agents, 139 answered, 1 transacted. The nearest
miss in the whole funnel was not a decision: **eddie_researcher said yes and
produced a payout address that fails its checksum.** Eight characters of base32.
Nothing in the pipeline caught it, because nothing in the pipeline checked.

Checksum validation is therefore not a feature of this tool. It is the reason
the tool exists. Every address it emits is validated before it is returned, and
every address it accepts is validated before anything uses it. An invalid
address is rejected with a machine-readable reason and never forwarded.

## Non-custodial by construction, not by assurance

There is no network call anywhere in this package. Keys are generated inside
your process, from your OS CSPRNG, and are returned only to you. Nothing is
transmitted, because nothing here can transmit.

That claim is enforced by a test, not by this paragraph:
`test_nothing_in_the_money_path_imports_a_network_module` fails the build if any
module in the money path ever imports `socket`, `http`, `urllib`, `requests`,
`ssl` or `asyncio`. Read the imports; then read the test.

## Install as an MCP server

```json
{
  "mcpServers": {
    "nano-wallet": {
      "command": "python3",
      "args": ["/absolute/path/to/nano_wallet/mcp_server.py"]
    }
  }
}
```

Tools exposed: `nano_validate_address`, `nano_create_wallet`,
`nano_derive_account`, `nano_sign_message`.

There is deliberately **no send tool and no balance tool**. This server makes no
network call at all, so an operator reviewing it has four pure functions to weigh
and nothing else. Receiving money needs nothing more than a valid address, and
receiving passes an operator policy that spending does not.

## Use from the command line

```
$ python3 cli.py check nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3
{ "valid": true, "public_key": "E89208DD...", ... }                        # exit 0

$ python3 cli.py check nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr1
{ "valid": false, "reason": "bad_checksum", ... }                          # exit 1

$ python3 cli.py new                       # a seed and an account, generated locally
$ python3 cli.py derive <seed_hex> 1       # account 1 from a seed you already hold
$ python3 cli.py raw 0.0000005             # exact XNO -> raw, integers only
```

`check` exits 0 for a valid address and 1 for an invalid one, so it drops into a
shell pipeline or a CI gate as-is.

## Use as a library

```python
import wallet

account = wallet.create_wallet()           # seed, private_key, public_key, address
verdict = wallet.validate(their_address)   # never raises; {"valid": bool, "reason": ...}
proof   = wallet.sign_message(b"challenge", account["private_key"])
```

`xno_to_raw` / `raw_to_xno` convert at Nano's full 30-decimal resolution using
integers only — a sub-cent per-call price does not round to zero and does not
drift under summation. No float appears in the money path.

## Verifying it before you trust it

```
$ python3 -m unittest discover -s tests
Ran 44 tests — OK
```

Four of those tests are known-answer vectors against facts this repository
cannot influence:

| vector | pins |
| --- | --- |
| all-zero public key → `nano_1111…hifc8npp` | the address codec |
| mainnet genesis key `E89208DD…` → `nano_3t6k35gi…oncuohr3` | the address codec, independently |
| RFC 8032 Ed25519 vector 1 (SHA-512) | the curve arithmetic and the signature |
| all-zero seed → `9F0E444C…` / `nano_3i1aq1cc…99d4r3b7` | Nano's BLAKE2b variant of that arithmetic |

The curve code takes its hash function as a parameter precisely so the public
RFC 8032 vectors can prove it before the Nano variant is trusted with money.

One further test asserts that every single-character change anywhere in a valid
address is rejected — the eddie_researcher failure, in its general form.

## What this tool does not do

It does not broadcast blocks, fetch balances, or talk to a node. Pairing it with
a node is a separate decision with a separate review, and this package does not
make it for you.
