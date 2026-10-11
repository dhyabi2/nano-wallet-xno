# nano-wallet-xno — audit 2026-10-11

The lens: can an agent hold, receive and pay XNO with this wallet today without
being hurt?

## What I checked

- Fast-forwarded to `main` (`0ae5d45`) and ran the suite from a clean checkout:
  **288 passed, 5 subtests** on 3.13. `selfcheck.py` reports 7/7 and correctly
  states that the receive-only profile cannot spend.
- `payments.send` end to end: the `NANO_WALLET_ALLOW_SEND` gate, address
  validation of both ends, the idempotency-key length bound, the
  `misconfigured_send_limit` refusal (the operator's limit is parsed inside a
  `try`, so a typo in it is named as the operator's problem rather than
  reported as a complaint about the agent's amount), `send_limit_exceeded`,
  the replay branch, the mandate `check` before anything is signed and `spend`
  reserving before the broadcast, `account_state_unsettled`,
  `insufficient_balance`, the never-received case, `_key_holding`'s
  duplicate-hash refusal, and the record-before-broadcast ordering that makes a
  lost reply a settlement rather than a second payment.
- The `account_info` frontier/balance pairing that #4 fixed: the guard against a
  balance read at a different height than the frontier is still in place as a
  defence for other `NanoNode` implementations.
- `wallet.xno_to_raw` / `raw_to_xno` round-trips, one-raw granularity, the
  30-decimal bound, and `payments._six`, which **pads** the fraction to six
  places with `ljust` and never truncates — so a sub-cent amount is reported
  exactly, not rounded to zero. No float appears on any amount on this path.
- `blocks.build_signed`, `work.py`/`prework.py`, `keystore.py`, `nanoaddr.py`,
  `mandate.py`, and the receive path.

## What I found

**One defect, on the send path: `wallet.xno_to_raw` validated its digits with
`str.isdigit()` and no `isascii()`, so strings that are not decimal numbers
were silently given a value and sent.**

`str.isdigit()` is true for every Unicode digit, and `int()` reads most of them.
Measured against `main` (`wallet.py:96`):

```
ACCEPTED '1'    -> 1000000000000000000000000000000 raw
ACCEPTED '٠.٥'  -> 500000000000000000000000000000 raw
ACCEPTED '１'    -> 1000000000000000000000000000000 raw
ACCEPTED '1٢3'  -> 123000000000000000000000000000000 raw
ACCEPTED '0.٥'  -> 500000000000000000000000000000 raw
refused   '²'    -> invalid literal for int() with base 10: '²'
```

`'1٢3'` — an ASCII 1, an Arabic-Indic 2 and an ASCII 3 — was read as **123 XNO**.
A JavaScript `/^\d+$/` counterparty refuses exactly these strings, so a quote
carrying one is a refusal on the seller's side and an amount on ours.

**The sharpest form of it is that the two halves of one install disagreed.**
This package's own `mandate.py` already refuses non-ASCII digits, in both
`parse_raw` (line 304) and `xno_to_raw` (line 323), with the reasoning written
out. `wallet.xno_to_raw` — the function `payments.send` actually parses the
agent's amount with — did not. So the operator-mandate layer and the wallet
layer of the same wallet held different views of what an amount is.

It moves real money. Driven through `payments.send` against the fake node, with
the account funded 10 XNO and the default 1.0 XNO limit, an agent asking to send
`"0.٥"` had **0.5 XNO leave the account**, with a block signed and published.
On a feeless, sub-second, irreversible rail there is nothing to undo that with.

## What I fixed

`fix/amounts-must-be-ascii-digits` — `wallet.xno_to_raw` now requires
`isascii()` as well as `isdigit()` on both the whole and the fraction part,
matching the check `mandate.py` in this same package already makes.

The change adds only a condition to an existing refusal. It narrows what is
accepted and widens nothing; it changes no amount, no destination, no rounding
and no key path.

**Evidence.** Four tests. With `wallet.py` reverted to `main`:

- `tests/test_nano_wallet.py::TestAmounts::test_rejects_non_ascii_digits`
  **fails** ("ValueError not raised").
- `tests/test_send_reconcile.py::...::test_an_amount_that_is_not_ascii_digits_sends_nothing`
  **fails** ("ToolError not raised") — and the assertions it carries record the
  loss: a block published and the balance down 0.5 XNO.

The other two are controls that pass before and after, so they prove the fix
does not over-refuse: every ASCII spelling the function took before still works
(`"1"`, `"0"`, `"0.5"`, `".5"`, `"1."`, one raw, and a padded `"  7.25  "`), and
an ASCII `"0.5"` through `payments.send` still sends, reporting `0.500000` and
moving exactly 5 * 10**29 raw.

With the fix, on the branch merged with `main`: **292 passed, 5 subtests** (was
288), `py_compile` clean on every module, `selfcheck.py` 7/7.

## What I could not verify

- **Anything against the live Nano network.** Every test here runs against
  `fakenode.py`; no block was broadcast to a real node, and no real account or
  key was used. The claim that 0.5 XNO "left the account" is measured on the
  fake node's ledger, which is where the suite measures all of its balances.
- **Whether any real caller has ever passed a non-ASCII amount.** The defect is
  in what the parser accepts, not in an observed incident; I have no log or
  report showing it happened in the wild.
- **The `²` case was already refused**, but by `int()` raising rather than by
  the validation — the caller in `payments.send` catches `ValueError` and turns
  it into `invalid_amount`, so it never escaped as a crash. After this change it
  is refused by the validation instead, which is the same outcome by a clearer
  route.
