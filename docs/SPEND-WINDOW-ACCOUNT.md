# Spend-window account: an allowance an operator grants once, enforced by the balance

**Design plus reference code. Nothing in this document or in `spend_window.py` moves money.** Every real send,
funding step and sweep is the operator's, done with the operator's own key; the code here only records and checks.

## The problem, in the agents' words

atlastr_oz (2026-10-25): Nano's "per-spend signature sidesteps the cache entirely, but it forces users to sign on every
transaction". 13 agents we have talked to say a human must approve each spend (`a_human_must_approve`) and 16 that they
cannot spend autonomously (`cannot_spend_autonomously`); one of them: "I can receive money and I can spend it, but only
when Dave tells me to". On USDC an operator approves once, for a cap. On XNO there is no permit, no approve and no cap
a counterparty can be granted once, so "one approval" turns into "the agent holds a live key", which is exactly what an
operator policy is written to forbid.

## The mechanism: the balance is the allowance

An account funded with **exactly** the approved amount can never spend more than that, because there is nothing above
the cap to spend. No contract, no revocation hook, no cached signature to steal. Revocation is one sweep of the remainder
back to the funder, and that is a block on a public ledger rather than a flag in anyone's file.

```
operator                               agent (purpose account)                     seller
   |  1. generate a NEW account for ONE purpose (own key, own machine)
   |  2. fund it with the ceiling, once          ------------------>  balance = ceiling
   |  3. record the window (spend_window.py open: purpose, ceiling, approval URL, expiry)
   |                                              4. spends inside the balance  ---->  payee
   |  5. record each send that already happened (spend_window.py record)
   |  6. close: exhausted, expired, or revoked (sweep the remainder back; record the sweep block)
```

What the operator approves once is *this account, this ceiling, this purpose, until this date*, in writing at a URL
anyone can read (`--approval-url` is required: an approval nobody can go and read is an assertion).

## What is enforced in code (and where)

| rule | enforced by | refusal code |
|---|---|---|
| ceiling is an integer string of raw, never a decimal or float | `spend_window.open` | `ceiling_not_integer_string` |
| the window address and every payee pass the Nano checksum | `spend_window` (uses `nanoaddr`) | `bad_address_checksum` |
| the approval is citable (absolute http/https URL) | `spend_window.open` | `approval_not_citable` |
| an expiry exists and is after now | `spend_window.open` | `no_expiry`, `expiry_not_after_now` |
| cumulative recorded spend never passes the ceiling | `spend_window.record` | `would_exceed_ceiling` (loud: it means the account held more than its ceiling) |
| optional payee allow-list and per-call cap | `spend_window.record` | `payee_not_allowed`, `above_per_call_cap` |
| a block is never counted twice | `spend_window.record` | `block_already_recorded` |
| revocation names the sweep block | `spend_window.close` | `revocation_without_sweep` |
| a refused record leaves the state file byte-identical | CLI saves only after every check passed | |
| per-call and rolling per-day send caps at the moment of signing | `capped_wallet.py` (this repo, v1.3.0), if the agent holds the key itself | the wallet refuses before signing |

The record cannot sign, send or sweep: it imports only `argparse`, `json`, `re`, `sys` and `nanoaddr`, and a test fails
if that changes. Two layers is the point: `capped_wallet` stops a send *before* it is signed; the funded balance stops
it even if a bug, a prompt injection or a changed config got past that, because the money is not there.

## What the operator does, step by step

1. Make a fresh account for the purpose (`python3 nano_keygen.py`, offline). Keep its key where the agent can reach it only if you
   want the agent to sign; otherwise keep it yourself and sign each batch (receive-only agents never need it).
2. Send the ceiling to it from your own wallet. This is the only funding step and it is yours.
3. `python3 spend_window.py open --purpose "..." --ceiling-raw <int> --address <that account> --approved-by "<you>" --approved-at <UTC> --approval-url <where you said so> --expires <UTC> --now <UTC>`
4. After each send the agent made: `python3 spend_window.py record <id> --block <hash> --amount-raw <int> --payee <nano_...> --now <UTC>`
5. End: `close <id> --reason exhausted|expired`, or `--reason revoked --sweep-block <hash>` after sweeping the remainder back.
6. `python3 spend_window.py feed --now <UTC> --out feed/spend_windows.json` for a byte-reproducible summary. It reports
   `cached_approvals_outstanding: 0` always, because there is no approval to cache.

## Limits, stated plainly

- **It is only a record.** It does not read the chain. Check a recorded block with `nano-settlement-verify` or
  `nano-invoice verify-leg`, and check the account's balance on any node; a record that disagrees with the ledger loses.
- **A funded key can still spend the whole ceiling** at once, to the wrong payee, if no allow-list is used. The ceiling is the
  worst case; choose it as the amount you can lose.
- **One purpose, one account.** Reusing the account for a second purpose makes the ceiling a lie. A buyer paying many
  sellers with pre-signed blocks (see `xno-call-binding/docs/PRESIGNED-SEND.md`) needs this separation anyway: one
  outstanding pre-signed send per frontier.
- **Receive-only comes first.** For agents that cannot hold a key at all, receiving passes an operator policy that sending
  does not; see the-unowned-rail `OPERATORS.md`.

## Tests

`python3 -W ignore -m unittest tests.test_spend_window` (15 laws: exact integer arithmetic to the ceiling, 1 raw over
refused with the file unchanged, float/decimal ceilings refused, no citable approval refused, expiry rules, revocation
needs a sweep block, the two literal claims on every row, checksum, honest-when-empty feed, no socket on the import graph).
