# nano-wallet-xno — code audit, 2026-10-01

Scope: the tree at `a1527db` (`main`). Baseline, before any change:

```
$ python3 -m unittest discover -s tests   # Ran 101 tests — OK
$ python3 e2e_check.py                    # 15/15 checks passed
$ python3 e2e_receive_only.py             # 16/16 receive-only checks passed
$ python3 selfcheck.py                    # 7/7 pass
$ python3 -m py_compile *.py tests/*.py   # clean
```

This run followed one question all the way down: when the wallet builds a send, where does each
number in that block come from, and is it from the same moment in time.

## Found and fixed — an agent could send more XNO than it asked to

`HttpNanoNode.account_info` (`nanonode.py:109`) read the node's answer like this:

```python
return {
    "frontier": answer["frontier"],                                            # the TIP
    "balance_raw": int(answer.get("confirmed_balance", answer["balance"])),    # the CONFIRMATION HEIGHT
    "representative": answer.get("confirmed_representative", answer.get("representative")),
    "confirmed": answer.get("confirmed_height") is not None,
    ...
}
```

`frontier`/`balance` describe the account's **tip**. `confirmed_frontier`/`confirmed_balance`
describe it at its **confirmation height**. They are two different points on the same chain and
they differ for the second or two after the wallet publishes a block of its own. This returned the
tip's frontier beside the confirmed balance.

`payments.send` (`payments.py:286-289`) then uses both, as a pair:

```python
previous = bytes.fromhex(info["frontier"])
signed = blocks.build_signed(..., previous, rep_pk, balance_raw - amount_raw, destination_pk, "send")
```

and Nano reads the amount of a send as `previous.balance - block.balance`. So the gap between the
two moments is paid out of the account, on top of the payment.

The sequence is the ordinary one for a paid agent — get paid, then pay:

1. the agent receives 1 XNO; `nano-wallet receive` publishes a receive block;
2. that block is not confirmed yet, so the tip holds 6 XNO while the confirmation height holds 5;
3. the agent sends 1 XNO.

Measured, driving the real `HttpNanoNode` against a recorded `account_info` answer of exactly that
shape:

```
account_info() returns
   frontier    = BBBB…BBBB   <- the unconfirmed tip, where the ledger holds 6 XNO
   balance_raw = 5 XNO       <- the confirmed balance
   confirmed   = True        <- and it said the account was confirmed

send 1 XNO builds  previous = BBBB…BBBB,  balance = 5 - 1 = 4 XNO
Nano computes      6 XNO - 4 XNO  =>  2 XNO LEAVES THE ACCOUNT
```

Two XNO for a one XNO payment — the agent pays out everything it had just been paid, as well as
what it meant to send. On a feeless, sub-second, irreversible rail there is nothing to undo it
with.

**Fixed** by reading one point in time: the tip's frontier with the tip's balance and the tip's
representative. The tip is the right pair, not the confirmed one:

- `previous` has to **be** the account's tip, or the block forks our own chain;
- the unconfirmed part of the tip is our own just-published blocks, not somebody else's rollback
  risk — `receivable` asks for `include_only_confirmed`, so every send this wallet receives has
  already been confirmed by the network.

`confirmed` now answers the question a caller was actually asking — is the account's latest block
confirmed — as `frontier == confirmed_frontier`. It used to be `confirmed_height is not None`,
which is true for every account that has ever been opened, including the one above whose tip was
unconfirmed. A node that reports no confirmed frontier at all cannot tell us, and keeps the answer
it gave before rather than having one guessed for it.

Proved both directions. Against the unfixed `nanonode.py`, 4 of the 6 new tests fail, including:

```
AssertionError: 2000000000000000000000000000000 != 1000000000000000000000000000000
  : a send of 1000000000000000000000000000000 raw would actually move
    2000000000000000000000000000000 raw
```

With the fix: **107 unit tests OK** (101 before), 15/15 e2e, 16/16 receive-only, 7/7 selfcheck,
`py_compile` clean. The README's published count moved 101 → 107.

**Why nothing caught this.** `fakenode.py` implements the `NanoNode` interface directly, so no test
in the repository ever went through `HttpNanoNode.account_info` — the one function that reads the
real node's answer, in the one module allowed to touch the network. The new tests close that gap
and open no socket: `_rpc` is replaced with a recorded answer, so `e2e_check`'s network invariants
and the selfcheck's socket guard are untouched.

## Checked and clean

- **The money path is integer raw throughout.** `blocks.py` states the rule and keeps it;
  `block_hash` refuses a non-`int` balance and range-checks it against the 128-bit raw field.
  `mandate.py`'s `parse_raw` refuses `float` and `bool` explicitly, refuses leading zeros and
  non-ASCII digits, and `load_signed` installs `parse_float=_refuse_float` so a non-integer number
  anywhere in a mandate file is refused rather than rounded.
- **The operator mandate.** Read end to end. The signed message is a domain tag plus the mandate
  hash, so a mandate signature can never double as a Nano block signature; the canonical form is
  sorted-key, separator-free, ASCII JSON; unknown fields are refused rather than ignored; the agent
  and operator must differ; `per_payment_max` may not exceed the cap; the ledger refuses to spend
  if its totals do not add up, if it belongs to another mandate, or if it cannot be read; a send
  that raises stays counted as spent, which is the right direction for a cap. `spend` reserves
  before broadcasting and writes atomically (`mkstemp` + `fsync` + `os.replace`) under an advisory
  lock.
- **`nanonode.py` handles the network properly** — `URLError`, `OSError` and `ValueError` all
  become `NodeError` with a stable reason, and `host_of` strips credentials from every message, so
  a `https://user:secret@node/` URL cannot reach a log line. (This is the defect I had just fixed
  in the sibling `nano-receipt-ledger`; this repository already had it right.)
- **Receive-only really is receive-only.** `selfcheck.py` proves address creation makes no network
  call by replacing `socket.socket` with something that raises, rather than asserting it did not,
  and the e2e run checks transitively that no module in the money path can import a network module.
- **No secrets in the tree.** The 64-hex strings are known-answer vectors — the all-zero seed, the
  mainnet genesis public key, RFC 8032 vector 1 — and `test.yml` explains, correctly, why this
  repository deliberately has no 64-hex guard where the sibling ledger does.

## Not verified

- **Against a real Nano node.** No socket was opened. The `account_info` answer the new tests use
  is a recorded shape, written from the node RPC's documented fields
  (`confirmed_balance`, `confirmed_frontier`, `confirmation_height_frontier`,
  `confirmed_representative`), not captured from a live node this run. The field *names* are the
  thing to confirm from a connected box; the arithmetic consequence does not depend on them.
- **The size of the window in practice.** How long a freshly published receive stays unconfirmed on
  mainnet was not measured. It does not change the defect — any gap at all is paid out of the
  account — but it decides how often this would have fired.
- **`pyproject.toml` declares `requires-python = ">=3.8"`** and nothing tests 3.8 or 3.9;
  `test.yml` says so itself. Unchanged by this run.
- **The publish workflow** was not read or touched, by rule.

## Addendum, 2026-10-03

Rebuilt on `main` after #5 merged. The only conflict was the README's published count — this branch
had 101 → 107, #5 moved it to 102, so the merged tree is **108**. Re-verified on the merge result,
not on the old head: 108 unit tests OK, `e2e_check` 15/15, `e2e_receive_only` 16/16, `selfcheck`
7/7, `py_compile` clean.

**One thing this change broke that the original audit missed.** `mcp_server.py:73` advertised the
`balance` tool as "Read an account's **confirmed** balance and what is waiting to be pocketed."
That was true of `main`, which returned `confirmed_balance`. Switching to the tip's balance left
the tool describing something it no longer returns — and an MCP tool description is exactly what an
agent reads to decide what it may spend, so a stale one here is a wrong number handed to the thing
making the decision. It now says the balance is the account's latest block and points at the
`confirmed` flag beside it, which answers whether the network has confirmed that block.

That was the whole blast radius of the switch outside `nanonode.py`: `grep` finds no other
"confirmed balance" claim in `mcp_server.py`, `README.md` or `cli.py`, and `info["confirmed"]` is
read in exactly one place (`payments.py:148`) and only to be reported back out of the `balance`
read-out. Nothing gates a send or a receive on it.
