# nano-wallet — a Nano (XNO) wallet as one install

No dependencies, no account, and a profile your operator can verify:

    create address · validate address (checksum) · balance · receive

Python 3.8+ and the standard library. Nothing else. `pip install` is not
required and there is no package to resolve — copy the directory, or clone it,
and run `python3 cli.py <command>` from inside it. If you want the `nano-wallet`
command used below on your PATH, one line installs it (no dependencies):

```
pip install git+https://github.com/dhyabi2/nano-wallet-xno
```

Every `nano-wallet <command>` in this README is the same as
`python3 cli.py <command>` from a clone.

**If your operator will not let you spend, install it receive-only.** In that
profile `send` is not registered at all — it is absent from `tools/list`, and
calling it is `-32601 method not found`, because the method genuinely does not
exist. One command proves it on your own machine before you ask anyone to
approve anything:

```
$ python3 cli.py selfcheck --profile receive-only     # or: nano-wallet selfcheck ...
...
selfcheck: 7/7 pass - this install can receive XNO and cannot spend it.
```

Jump to [The receive-only profile](#the-receive-only-profile).

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

Keys are generated inside your process, from your OS CSPRNG, and are returned
only to you. **No key, seed or signature is ever transmitted**: blocks are built,
hashed and signed locally, and the node is handed an already-signed block. There
is nothing to trust us with, because we are never given anything.

Address creation and validation make **no network call at all** — not a
preference, a property: the modules they are built from cannot reach the
network, transitively. `balance` and `receive` do need a node, and reach one
through exactly one file, `nanonode.py`. That is the whole of what leaves your
machine, and you can read it in a sitting.

Both claims are enforced by tests rather than by this paragraph.
`test_nothing_in_the_money_path_imports_a_network_module` walks the import graph
of the offline core and fails the build if `socket`, `http`, `urllib`,
`requests`, `ssl`, `asyncio` or `ftplib` is reachable from any of them — and
then asserts the exact set of modules that *can* open a connection, so adding a
network call anywhere new turns the suite red.

Money is an integer count of raw end to end (1 XNO = 10\*\*30 raw). No float
touches a balance, an amount or a comparison; a double holds 53 bits of mantissa
and a raw balance needs up to 128, so one float round trip would silently round
away real money. `test_a_float_balance_is_refused` makes that a build failure
rather than a convention.

## Install as an MCP server

```json
{
  "mcpServers": {
    "nano-wallet": {
      "command": "python3",
      "args": ["/absolute/path/to/nano_wallet/mcp_server.py",
               "--profile", "receive-only"],
      "env": { "NANO_WALLET_ALLOW_SEND": "0" }
    }
  }
}
```

Drop `--profile receive-only` to get the full surface, where `send` is
registered and refused unless `NANO_WALLET_ALLOW_SEND=1`.

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

## Receiving against a public node

A new wallet owns nothing, so its first operation is a receive, and the only
node it has is a public one. Public nodes refuse `work_generate`. Nano asks
**64x less work of a receive or an open block than of a send**
(`0xfffffe0000000000` against `0xfffffff800000000`), and that much is findable
right here, so the refusal is no longer the end of the road: `balance` and
`receive` work out of the box against an endpoint that does nothing but read
and publish.

* The node is still asked first — one that generates work is faster.
* Local work is found **only for a receive or an open block**. Nothing that
  spends falls back: at the send threshold this takes minutes, and a send whose
  node owes it work says so instead of stalling. Local work can never put a
  send on the network under-worked at the receive threshold.
* Measured here at about 1.4M hashes a second: a receive averages 2²³ hashes,
  a few seconds to about twenty. The budget is a wall clock (60s by default),
  so it fails closed with `work_unavailable` rather than hanging.
* Pass `local_work=False` to `HttpNanoNode` to require the node to supply work.

The validator is pinned in `tests/test_work.py` against a real open block from
the Nano mainnet — `BCE621224274F7DCB1B9BFB212CF9E5CADC50B47E8BEA5C2E4ED0F063566D85B` —
whose work the live network accepted. Of the six plausible byte orderings
exactly one validates it, and it clears the receive threshold while failing the
send threshold, which is what a receive block's work should do.

## The receive-only profile

An operator who will not approve spend authority is not being difficult. They
are being asked to trust a claim they cannot check. This profile turns the claim
into something they can check in one command.

**The profile is enforced in the server, not by the absence of an environment
variable.** Under `--profile receive-only`:

* `tools/list` returns exactly `create_address`, `validate_address`, `balance`,
  `receive`. Nothing that can move money out is in it.
* calling `send` is `-32601 method not found` — not a `403`. A 403 would tell
  your operator the capability is present and merely switched off.
* starting it with `NANO_WALLET_ALLOW_SEND=1` **fails at startup**, exit 2, and
  serves nothing. A profile a stray environment variable can widen is not a
  profile.

### The three things to show your operator

```
$ nano-wallet selfcheck --profile receive-only

[pass] tools registered                       create_address, validate_address, balance, receive
[pass] tool 'send' is NOT registered          absent from tools/list
[pass] known-good address validates           nano_1111111...hifc8npp
[pass] altered address rejected               reason=invalid_checksum
[pass] created a new address offline          nano_373i  (no network call was made)
[pass] private key absent from all output     none in any field
[pass] send authority: unavailable in this profile not registered

selfcheck: 7/7 pass - this install can receive XNO and cannot spend it.
```

It exits 0 only if every check holds, and it checks the install it is running
on. Point it at a machine configured for the full profile and it says so:

```
$ NANO_WALLET_PROFILE=full nano-wallet selfcheck --profile receive-only
...
selfcheck: 4/7 pass - this install is NOT receive-only.          # exit 1
```

Second, the capability document — `nano-wallet capabilities --profile
receive-only`, and the same bytes over HTTP at
`GET /.well-known/nano-receive-only` (`nano-wallet wellknown`). Its
`tools_registered` field is built from the same tuple the server registers from,
and the test suite asserts the document against a live `tools/list` rather than
against a constant, so it cannot drift from what the server actually serves.

Third, the sentence to paste. It is fixed in code, not templated per recipient:

> This agent may generate a Nano (XNO) receiving address and accept incoming
> payments. It cannot send, spend, or sign outgoing transfers. No key leaves
> this machine.

### The four steps

```
1. python3 mcp_server.py --profile receive-only      (or the mcp.json above)
2. python3 cli.py selfcheck --profile receive-only   read the last line out
3. create_address                                    hand over the address
4. claim a job on the work queue, and get paid
```

Step 4 is what makes 1–3 worth doing. **Do not propose this onboarding without a
live job to claim** — an empty queue turns it into homework.

## Let your operator set a spend cap once

"Standing latitude to operate, but not to spend" is where most agents stop.
An **operator mandate** turns it into a permission the operator signs once,
with the same Nano key that holds their XNO: which agent may spend, a total
cap and a per-payment max in raw integer strings (1 XNO = 10\*\*30 raw), an
optional payee allow-list, a required `purpose` saying what the spend buys,
and an expiry. `mandate.py` is one standard-library file (vendored,
byte-for-byte, from `agent-wallet-multirail`; a test pins the bytes).

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install "git+https://github.com/dhyabi2/nano-wallet-xno"
mandate keygen --out operator.key   # demo operator key (mode 600); a real operator uses their own
mandate create --operator-key operator.key --agent nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7 --total-cap-xno 0.5 --per-payment-max-xno 0.01 --purpose "Web-search API calls for the research task" --days 30 --out mandate.json
mandate verify mandate.json
mandate status mandate.json         # remaining cap, from mandate.json.ledger.json
```

(The agent address above is a well-known test address - put your agent's
there.) Then start the full profile with the mandate:

```
NANO_WALLET_ALLOW_SEND=1 NANO_WALLET_MANDATE=/abs/path/mandate.json python3 mcp_server.py --profile full
```

Every `send` from the mandate's agent is then checked - signature, agent,
expiry, per-payment max, payee allow-list, remaining cap - before a block is
signed, and reserved in the ledger before it is broadcast. A refusal is
`mandate_refused` (403) with `mandate_reason` (`cap_exhausted`,
`over_per_payment_max`, `payee_not_allowed`, `expired`, `bad_signature`,
`wrong_agent`, ...), and nothing is broadcast. A configured mandate that
cannot be read or verified refuses; it never falls back to sending uncapped.
`NANO_WALLET_REQUIRE_MANDATE=1` refuses every send that has no mandate.
`NANO_WALLET_MANDATE_LEDGER` moves the ledger. A broadcast that fails stays
counted, because it may have landed.

The ledger is local: it stops this runtime from overspending, not someone
with shell access who deletes it. For a hard ceiling, also fund the agent's
account with no more than the cap.

**Audit (2026-09-27):** before this change the only spend control was the
per-call limit `NANO_WALLET_MAX_SEND_XNO` (default 1.0 XNO), which is enforced
in code as documented. There was no cumulative cap, no payee allow-list and no
expiry; the README never claimed one. The per-call limit still applies on top
of any mandate.

## Verifying it before you trust it

```
$ python3 -m unittest discover -s tests
Ran 208 tests — OK

$ python3 e2e_check.py
15/15 checks passed

$ python3 e2e_receive_only.py
16/16 receive-only end-to-end checks passed
```

The two `e2e_*` scripts drive the real entry points — the MCP server as a
subprocess over stdio, the CLI as a subprocess, the well-known document over a
real HTTP request — rather than calling the functions behind them. No check
anywhere reaches a Nano node: `fakenode.FakeNode` keeps a real per-account chain
and rejects a fork, so a test that builds an invalid chain fails the way a node
would fail it.

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

It does not choose a representative for you — a new account represents itself,
because that involves no third party. Set `NANO_WALLET_REPRESENTATIVE` to
override, and an account that already has one keeps it.

It does not run a node. `balance` and `receive` need one reachable at
`NANO_NODE_URL`, including for proof-of-work, and without it they return
`node_unreachable` (503) naming the node's host and never its credentials.

It does not send anything under `--profile receive-only`, and under the full
profile it will not send without `NANO_WALLET_ALLOW_SEND=1`, above
`NANO_WALLET_MAX_SEND_XNO` (default 1.0 XNO per call), outside an operator
mandate when one is configured, or twice for one `idempotency_key`.

When a send's reply is lost (`node_unreachable` after the block was handed to
the node), retry the same call with the same `idempotency_key`. The wallet
settles it from the ledger and never builds a second send: the block it signed
is on the ledger (`reconciled: "landed"`); it is not, but the account has not
moved since, so that same signed block is published again - one hash, at most
one payment (`reconciled: "rebroadcast"`); or a different block was built where
it would have gone and is confirmed, so it can never land and nothing was paid
(`send_did_not_land`, 409 - retry under a new key). If the node cannot be asked,
cannot show which block was built there, or that block is not confirmed yet (a
fork ours may still win), the answer stays `send_outcome_unknown` (409); do not
switch keys while it is. The account's frontier is read first, so the first two
cases settle on any node; looking the block up (`block_info`) is needed only
once the account has moved past it, and a node that refuses that call, or a
custom `NanoNode` without it, leaves only that case unknown - the 409 names the
block's explorer link so it can be checked by hand. A send the operator mandate refused leaves nothing for a
retry to publish, and a second key asking for the identical block (same terms,
same frontier) is refused with `duplicate_send` (409) naming the first key.
A block the node rejects as invalid (bad work, bad signature, a balance that does
not add up) can never land, so it holds nothing: the answer is `send_rejected`
(422), nothing was paid, and retrying the same call signs a new block. Under an
operator mandate its reservation stays counted against the cap.
