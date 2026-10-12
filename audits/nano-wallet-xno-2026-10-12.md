# nano-wallet-xno — audit 2026-10-12

Same lens: can an agent hold XNO and pay — or get paid — with this wallet today without
being hurt? This pass was run on `main` after #18 (`1fa1785`, "an XNO amount must be a str,
not a JSON number") merged, so the tree audited already refuses a float amount from the
agent's side. The question this pass asked is the mirror of #18: the agent's amount is now
checked, but **what about the amounts the node sends back?**

## What I checked

- The suite on `main`: **297 tests, OK** (`python3 -m unittest discover -s tests`), plus
  `python3 e2e_check.py` — **20/20 checks passed**. Both are what `README.md:331-336`
  promises.
- `payments.send` end to end (`payments.py:225-400`): the send gate, the amount conversion,
  the operator limit, the idempotency replay, the mandate guard, the account read, the block
  build, and the record-before-broadcast ordering.
- `payments.receive` (`payments.py:150-220`) and the receivable list it builds blocks from.
- `nanonode.HttpNanoNode` (`nanonode.py`) — the one module in the package that touches the
  network — every field it reads off a node answer.
- `wallet.xno_to_raw` / `raw_to_xno` for float use anywhere on an amount: none. The only
  `float()` and `round()` calls in the package are in `work.py` and `cli.py`, on work rates
  and benchmark timings, never on an amount.

## What is right, and worth recording

- **`account_info` asks for the representative** (`nanonode.py:223`,
  `"representative": "true"`), and `_representative_pk` carries the account's existing one
  into both the send and the receive block. This is the correct shape of a defect found in
  the twin repositories today (`dhyabi2/nano-mcp#30`, `dhyabi2/nano-mcp-public#31`), where the
  flag is missing and every block silently re-delegates the account. This wallet does not have
  that defect; it is the reference for how the others should read.
- The frontier and the balance are read as one pair from the tip (`nanonode.py:220-267`), with
  `payments.send` keeping #10's refusal as a guard against a later mixed pair.
- A send whose node reply never arrived is settled from the ledger rather than replayed
  (`payments.py:292-301`), and the attempt is recorded before the broadcast.

## What I found and fixed

**A raw amount the node sent as a JSON number was read with a bare `int()`, and the send
path builds a block out of it.**

`nanonode.py:252` read the account's balance as `int(answer["balance"])`, and
`nanonode.py:281,284` read each receivable amount the same way. `int()` accepts more than a
Nano node sends:

- a JSON **number**: raw carries 31 digits where a float64 holds about 16, so a balance of
  `6900000000000000000000000000000` raw arriving as `6.9e30` comes back as
  `6899999999999999883876432347136` — **116,123,567,652,864 raw low**;
- `true`, read as 1 raw;
- a **non-ASCII digit** string (`"٣"` is 3), which `str.isdigit()` allows — the exact pair
  of checks `wallet.xno_to_raw` already applies to the agent's own amount
  (`wallet.py:108-118`) and the reason it applies both.

This is not a display number. `payments.send` builds the block's new balance as
`balance_raw - amount_raw` (`payments.py:357`), while Nano reads the amount a send moved as
the **ledger's** `previous.balance - block.balance`. The rounding is therefore not cancelled
out by appearing on both sides of the subtraction. Measured on exactly that answer before the
fix: asking to send 1 XNO signs and publishes a block with balance
`5899999999999999883876432347136`, so the ledger reads
**1.000000000000000116123567652864 XNO** moved. A value that rounds the other way pays the
seller short instead. There is no fee and no reversal to undo either with.

A receive is luckier — a receive block whose balance does not credit the source send exactly
is rejected by the node — so the loss there is a refusal, not a wrong amount. It is guarded
anyway, in the same place and for the same reason.

**The fix is a refusal, not a conversion** (`nanonode.raw_from_node`): a reply that does not
carry raw as ASCII digits is not a usable answer about this account, so it raises
`NodeError("node_error")`, which `payments.send` maps to a 503 the caller holds and retries.
Nothing is signed or published.

- `tests/test_node_raw_must_be_digits.py`: 10 checks, **7 failures and 1 error on `main`**,
  all 10 passing with the change, including the end-to-end one that pins "nothing may be
  signed or published" and its control, "a digit balance still moves exactly what was asked".
- Whole suite after the change: **307 tests, OK**; `e2e_check.py` 20/20;
  `README.md:333` updated from 297 to 307.

## Could not verify from here

- No live node: this container's proxy answers 403 for `rpc.nano.to`, so every node answer in
  this audit is the suite's in-memory fake or a patched `urlopen`. Whether any node or proxy
  in front of one actually emits raw as a JSON number was **not** established — the fix is a
  type refusal on the wallet's own trust boundary, not a report about a particular node.
- Nothing was broadcast and no funded wallet exists here, so the send path was exercised only
  up to the captured `process` call.
