# nano-wallet-xno — audit 2026-10-02

Scope: can an agent hold, receive and pay XNO with this wallet today, without being hurt.
Last audited 2026-09-27; the mandate work (#3) and the two README fixes (#1, #2) have landed
since.

Read end to end: `payments.py` (`balance`, `receive`, `send`), `blocks.py`, `wallet.py`
(`xno_to_raw` / `raw_to_xno`), `nanoaddr.py`, `nanonode.py`, `mcp_server.py`'s error
mapping, `keystore.py`, `profiles.py`, `README.md`, `agent-skill.json`.
`python3 -m pytest -q` on `main`: 101 passed, 1 subtest. With this change: 102 passed, 5
subtests.

## Checked

- Amount handling: `xno_to_raw` / `raw_to_xno` are exact integer raw in both directions,
  refuse a negative, a second decimal point, non-digits and more than 30 decimals. There is
  no float on any amount in `payments.py`, `blocks.py` or `wallet.py`.
- `blocks.block_hash` rejects a non-integer balance, a balance outside the 128-bit raw
  field, and any field that is not 32 bytes, before anything is signed.
- The send gate (`NANO_WALLET_ALLOW_SEND`), the per-call limit, the idempotency key
  (replay returns the first result; a reuse with different parameters is a 409 conflict),
  the balance check, and the mandate guard's check-then-reserve ordering.
- `receive` signs locally and hands the node an already-signed block; the key never leaves
  the process.
- That a bad address makes no network call, in `balance` and in `send`.

## Found and fixed (this PR)

**An operator typo in the send limit was reported to the agent as a bad amount.**
`payments.py:240`

```python
limit_raw = wallet.xno_to_raw(env.get("NANO_WALLET_MAX_SEND_XNO") or DEFAULT_MAX_SEND_XNO)
```

The operator's limit is parsed with the same exact parser as the agent's amount, and that
call sat outside every `try`. So `NANO_WALLET_MAX_SEND_XNO="0.5 XNO"` — or `"1,5"`,
`"1e-3"`, anything that is not a bare decimal — made a perfectly good
`send(amount_xno="0.1")` raise

```
ValueError: amount is not a decimal number: '0.5 XNO'
```

`mcp_server` catches `ValueError` and returns its text, so the agent is told its amount is
not a decimal number, naming a value it never sent. The only move that error suggests is a
different amount, and no amount can work: every send from the install is refused until a
human notices. The limit is also the one thing standing between an agent and a larger
payment than the operator allowed, so a value that cannot be parsed must not be guessed at
either.

Fixed: the parse is wrapped and raises `ToolError("misconfigured_send_limit", …, 403)`
naming the variable and its value, and saying that retrying with another amount will not
help. Nothing is signed or published.

Evidence: `tests/test_receive_only.py::SendIsGated::`
`test_a_misconfigured_send_limit_is_named_and_not_blamed_on_the_amount` drives the real MCP
server against `fakenode` with a funded account and four bad limits. All four subtests fail
on the previous code (the bare `ValueError` escapes) and pass with the change; the test also
asserts nothing was published in each case.

## Verified, waiting on the owner

**PR #4 — `account_info` paired the tip's frontier with the confirmed balance — is real,
and it is the most serious thing open on this repository.** Re-read independently this run:
`nanonode.HttpNanoNode.account_info` returned `frontier` (the tip) beside
`confirmed_balance`, and `payments.send` uses the first as `previous` and the second to
compute the new balance (`balance_raw - amount_raw`). Nano reads the amount of a send as
`previous.balance - block.balance`, so while a receive of ours is still unconfirmed the two
disagree and the difference leaves the account: the agent asks to send 1 XNO and more than
1 XNO goes. Its branch is green here (107 passed). It touches the code that sends money, so
it is not merged by a routine; it deserves a look soon.

## Could not verify

- No XNO moved: everything above ran against `fakenode`, and no live node, wallet or
  mandate file was touched.
- Every external URL in `README.md` and `agent-skill.json`, including the
  `https://nanolooker.com/block/<hash>` receipt link `send` hands back on every payment.
  This environment's network policy denied the hosts at the proxy (CONNECT 403), so whether
  that receipt link still resolves is untested. It is the one piece of the send result an
  agent is expected to pass to a human.

## Noted, not changed

`balance()` reports `balance_xno` from whatever `account_info` returns and a `confirmed`
flag beside it. With PR #4 in, that becomes the tip balance plus an honest `confirmed:
false` when the tip is unconfirmed — which is the right pair, but an agent that reads only
`balance_xno` can still treat an unconfirmed balance as spendable. Worth a look together
with #4 rather than separately.
