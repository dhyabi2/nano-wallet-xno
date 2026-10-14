# nano-wallet-xno — audit 2026-10-14

Lens: can an agent hold XNO and pay — or get paid — with this wallet today without being
hurt?

Starting point: `main` at `HEAD`. **Nothing changed this run.** No defect I could prove, and
no open pull request on this repository. This note records what was checked so the next run
does not re-tread it.

## The repository's own three claims, all verified first-hand

`README.md:330-338` tells a reader to run three commands before trusting the wallet. All
three were run, and all three print exactly what the README says they print:

| command | README claims | measured |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | `Ran 307 tests — OK` | **307 tests, OK** |
| `python3 e2e_check.py` | `20/20 checks passed` | **20/20 checks passed** |
| `python3 e2e_receive_only.py` | `16/16 receive-only end-to-end checks passed` | **16/16 passed** |

The 10-12 audit recorded 297 tests against a README that then said 297; tests have been added
since and the README was kept in step. The count is right today.

Two of the `e2e_check` assertions are worth naming because they are the custody claim stated
as something checkable rather than asserted, and they pass:

```
ok    no module in the money path can reach the network, transitively
ok    nanonode.py is the ONLY module in the wallet that touches the network
```

## What I read

- `payments.send` end to end (`payments.py:225-420`): the send gate
  (`NANO_WALLET_ALLOW_SEND`), the amount conversion, the operator per-call limit, the
  idempotency replay, the mandate guard, the account read, the block build, and the
  record-before-broadcast ordering. The ordering is right for a feeless irreversible rail: the
  attempt is written before the broadcast, so a lost node reply is settled from the ledger by
  republishing the same block (one hash, so at most one payment) rather than rebuilt from a
  new frontier and paid twice.
- `wallet.xno_to_raw` and `raw_to_xno`. No float anywhere on an amount. The function refuses a
  non-`str` outright (so a JSON number cannot arrive as a float), and checks `isascii()` as
  well as `isdigit()` — without which `"1٢3"` read as 123. A zero amount is refused
  downstream by `payments.send`'s `amount_raw <= 0`, which is the guard that catches
  `xno_to_raw(".") == 0`.
- **`mandate.py`'s cap enforcement**, the part earlier audits named but had not read through:
  `validate_fields` → `verify_signed` → `MandateGuard._read` → `_check` → `spend`. The order
  is correct and the reservation is right: `spend` takes an exclusive `flock`, re-verifies the
  signed mandate, re-reads the ledger, checks per-payment max / payee allow-list / remaining
  cap, **writes the reservation, and only then calls `send()`** — and on any exception marks
  the entry `unknown` rather than removing it, because a cap that forgets an unknown payment
  can be overrun. `per > total` is refused at validation, the operator and agent must differ,
  and the mandate's hash is checked against its content before the signature is checked
  against the operator's key.
- `_representative_pk` carries the account's existing representative into both the send and
  the receive block, and `nanonode.account_info` asks for it (`"representative": "true"`).
  This is the defect the twin repositories had; re-confirmed today that `nano-mcp-public` has
  since fixed it on `main` (`e3d039a`, #31) and that `dhyabi2/nano-mcp#30` is still open with
  it. This wallet remains the reference for how the others should read.

## Two observations, neither worth a change

- **The ledger's integrity check uses `str(...).isdigit()`**
  (`mandate.py:567`): `spent = int(data["spent_raw"]) if str(data["spent_raw"]).isdigit() else None`.
  `str.isdigit()` is true for every Unicode digit, which is the exact hazard this codebase
  fixes deliberately in `wallet.xno_to_raw` (`isascii()`), in `mandate.parse_raw`, and in the
  sibling repositories' amount patterns. It is **not** a defect here: the ledger is a local
  file, and anyone who can write a Unicode digit into it can equally write `"spent_raw": "0"`
  and clear the cap outright. The Unicode path grants no capability the write itself does not.
  Recording it so a future reader does not mistake it for the same bug and does not "fix" it
  as though it were load-bearing.
- **`payments.send` records `to` as the caller spelled it**, not normalised, so the
  idempotency conflict check compares raw address strings. The same account presented once as
  `nano_…` and once as `xrb_…` under one key is refused `idempotency_conflict` rather than
  recognised as a replay. The refusal is the safe direction — nothing is sent twice — and the
  destination itself is compared as a public key (`destination_pk`), so no two different
  accounts can collide. A confusing 409 at worst; not a money defect, and tightening it would
  widen what a replay accepts.

## Could not verify

- **No live node, and no XNO moved.** Everything above is against `fakenode.FakeNode`, which
  keeps a real per-account chain, and the two `e2e_*` scripts driving the real MCP server and
  CLI as subprocesses. A send to the live network is the owner's call, not a routine's.
- Block signing is exercised against this package's own `ed25519_blake2b.py` and its vectors,
  not against an independent implementation.
