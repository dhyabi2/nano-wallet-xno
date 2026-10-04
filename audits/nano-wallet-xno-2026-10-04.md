# nano-wallet-xno — audit 2026-10-04

Lens: can an agent hold, receive and pay XNO with this today without being hurt?

Baseline on `main` (`9655dba`): 145 unit tests, `e2e_check` 15/15, `e2e_receive_only` 16/16,
`selfcheck` 7/7. After this branch: **152 unit tests**, and the three scripts unchanged at 15/15,
16/16 and 7/7.

## Checked

- `payments.py` end to end: `create_address`, `balance`, `receive`, `send`, the spend gate, the
  send limit, the idempotency store and the operator-mandate hooks.
- `nanonode.py`: `account_info`, `receivable`, `work_generate`'s local fallback, `process`, and the
  4xx/5xx split that decides whether a refusal may fall back to local work.
- `work.py`: the blake2b-64 reverse-byte difficulty, the receive/send thresholds, and the refusal to
  attempt the send threshold locally.
- `wallet.py` and `mandate.py`'s amount parsers, compared against each other.
- Every command the README prints: `cli.py selfcheck/check/new/derive/raw`, both `check` exit codes,
  and the three verification scripts.
- `keystore.py` file modes (`O_EXCL`, `0o600`), and a scan for `os.system`, `subprocess`, `eval` and
  unchecked path joins in the shipped modules: the only `subprocess` use is in the two `e2e_*`
  scripts, driving `sys.executable` with a literal argv.
- Secret scan of the tree: nothing.

## Found and fixed — a lost reply let the retry pay twice

`payments.send` recorded the idempotency key **after** the node's reply came back
(`payments.py:364` on `main`). `node.process` raising says the **reply** is missing, not the block:
Nano has no fee and no reversal, so a broadcast whose outcome is unknown may well have landed. The
error the caller sees is `node_unreachable` with **status 503** — the one error shape that invites a
retry.

That retry found no record of the key, rebuilt the block from the account's **new** frontier, and
paid the destination a second time. Measured with a node that applies the block and then loses the
reply:

```
attempt 1: refused node_unreachable
attempt 2: sent, block 72145213, replayed=False

blocks the node accepted:  2
XNO actually moved:        2        <- the agent asked to send 1
idempotency keys used:     1 ('one-and-only-key')
```

The same hole also bypassed the existing `idempotency_conflict` check: with no record, a retry under
the same key with a **different amount** was not a conflict either, it was a second send.

An operator mandate does not stop it. `MandateGuard.spend` reserves before broadcast and keeps an
unknown outcome counted — correct for the cap — but it has no notion of a duplicate `ref`, so the
second send is *charged* to the cap rather than refused.

**The fix only adds a refusal.** The attempt is recorded in `sent[key]` with `result: None` and the
signed block's hash **before** the broadcast — the same reserve-then-spend order `mandate.py`'s
ledger already uses, for the reason that file already gives ("under-counting is how a cap gets
overrun") — and a replay that finds an attempt with no result is refused `send_outcome_unknown`
(409), naming the block so it can be looked up on the ledger. Nothing about the amount, the
destination, the rounding or any key path changes.

A fresh `idempotency_key` still sends, so a timeout does not brick the wallet; a completed send
still replays with `replayed=True` and the same `block_hash`.

Five new tests fail on `main` and pass here; two more are controls that pass either way. Mutating
the guard to `if False and ...` fails four of the five.

## Found, not fixed here

- **`balance` reports `confirmed: true` for an account whose latest block is not confirmed.**
  `account_info` derives it from `answer.get("confirmed_height") is not None`
  (`nanonode.py:165`), and a node asked with `include_confirmed=true` returns `confirmed_height` for
  **every** opened account, confirmed tip or not. Measured against a node answer with
  `frontier=BBBB…`, `confirmed_frontier=AAAA…`, `block_count=7`, `confirmed_height=6`:
  `balance_is_frontier_balance` is correctly `False` while `confirmed` is `True`. An agent that
  waits for `confirmed` before it treats a payment as final is told yes too early.
  **Open PR #4 already fixes exactly this line** and says so in as many words, so this audit records
  it rather than opening a second pull request over the same three lines. #4 is held because it also
  changes which balance a send is built from, which is not a refusal.
- **The vendored `mandate.py` copies have drifted.** `agent-wallet-multirail`'s copy carries
  operator **revocation** (`sign_revocation`, `verify_revocation`, `check_revocation`, the `revoke`
  CLI) and an empty-string refusal in `xno_to_raw`; this repository's copy has neither, and
  `tests/test_mandate.py` pins its sha256, so the drift is deliberate rather than accidental. The
  consequence is worth stating plainly: a mandate enforced by **this** wallet cannot be withdrawn
  before it expires. Nothing here claims it can — the word "revoke" appears nowhere in this
  repository — so adding it would be a feature, not a fix, and it is left to the owner.
- **`wallet.xno_to_raw` accepts non-ASCII digits; `mandate.xno_to_raw` in the same tree refuses
  them.** `wallet.xno_to_raw("٥")` returns 5 XNO in raw and `wallet.xno_to_raw("१.५")` returns 1.5
  XNO, while `mandate.xno_to_raw` refuses both (it checks `.isascii()`, `wallet.py:96` does not).
  An ASCII `/^[0-9]+(\.[0-9]+)?$/` peer refuses what this wallet pays. The cap still holds (both the
  amount and `NANO_WALLET_MAX_SEND_XNO` go through the one parser, and the guard re-parses an int),
  so no amount is wrong today; it is an inconsistency between two parsers on the money path, kept out
  of this branch so the refusal above lands on its own.
- **`balance`'s `pending_xno` is capped at 64 receivable blocks** (`MAX_RECEIVE_BLOCKS`), so an
  account with more waiting reports less than it has. Nothing documents it as exhaustive.

## Could not verify

- No live Nano node: this environment's network policy does not reach one, and `e2e_check` pins that
  `nanonode.py` is the only module that may open a socket. The node answers in every test are
  literals or `fakenode.FakeNode`, which keeps a real per-account chain and rejects a fork.
- The RPC field names `confirmed_balance`, `confirmed_frontier` and `confirmed_height` are read from
  the documented node response, not captured from a node.
