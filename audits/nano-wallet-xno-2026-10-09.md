# nano-wallet-xno - audit 2026-10-09

Read through one question: can an agent hold and pay XNO with this, today, without being hurt?

HEAD audited: `0c70202` on `main`. Python 3.11.17, standard library only.

**Nothing was changed.** No defect was found that is worth a patch. The two open pull requests of
ours (#4 and #13) are the owner's by the money-send rule and were deliberately not touched or
re-commented; see the end of this file.

## Checked

- **The suite.** `python3 -m pytest -q` -> **158 passed, 5 subtests passed**.
  `python3 selfcheck.py` -> **7/7 pass - this install can receive XNO and cannot spend it.**
- **The README's headline install, which is the first step a new agent takes.**
  `pip install git+https://github.com/dhyabi2/nano-wallet-xno` was run into a fresh venv from the
  public clone URL the README prints: it resolves, builds and installs with no dependencies, and
  puts a working `nano-wallet` on PATH. `nano-wallet selfcheck --profile receive-only` from that
  install prints the same 7/7 and exits 0. Packaging is the kind of break a suite run from the
  repository root cannot see, so it was exercised rather than assumed.
- **Every command and every figure the README prints, run verbatim.**
  `cli.py check <good address>` -> `"valid": true` with the public key, exit 0.
  `cli.py check <same address, last character altered>` -> `"reason": "bad_checksum"` and the
  message naming both the carried and the implied checksum, exit 1.
  `cli.py raw 0.0000005` -> `"raw": "500000000000000000000000"`, exact.
  `NANO_WALLET_PROFILE=full nano-wallet selfcheck --profile receive-only` -> **4/7 pass - this
  install is NOT receive-only**, exit 1, which is the exact count and wording the README advertises
  for that case. The three failing rows are the three that should fail.
- **The mandate, which is the layer that refuses a payment.** `MandateGuard.spend` re-verifies the
  signature on every call rather than trusting construction, holds an `flock` across the whole
  check-reserve-send sequence, writes the reservation **before** calling `send`, and on any
  exception leaves the row counted as `unknown` - so a send whose outcome nobody knows still
  consumes the cap. `_read` refuses a ledger belonging to another mandate (`ledger_mismatch`), a
  `spent_raw` that is not a digit string, and any ledger whose payment rows do not sum to its own
  total (`ledger_corrupt`); an unreadable ledger is `ledger_unreadable` and a refusal, not a
  zero. `validate_fields` refuses unknown fields rather than ignoring them, refuses
  `per_payment_max_raw > total_cap_raw`, refuses an agent equal to the operator, and refuses a
  JSON amount that is not a string. `load_signed` passes `parse_float=_refuse_float`, so a
  non-integer number anywhere in the document is a refusal at parse time. Fails closed throughout.
- **The address codec, by arithmetic rather than by test name.** `public_key_from_address` requires
  60 characters after the prefix and `rest[0] in "13"`. In Nano's base32 alphabet
  `13456789abcdefghijkmnopqrstuwxyz`, `'1'` decodes to 0 and `'3'` to 1 - confirmed by indexing
  the alphabet directly - so the first character carries exactly the four zero pad bits plus the
  top bit of the key, 52 characters are exactly 4 + 256 bits, and the decode is a bijection. The
  checksum is then recomputed from the decoded key and compared. There is no second address string
  that decodes to the same public key, which was the specific thing this check set out to rule out.
- **Amount handling.** `parse_raw` refuses a `float` and a `bool` by type, refuses a
  non-digit or non-ASCII string, refuses a leading zero, and bounds the value to
  `1 .. 2**128-1`. `xno_to_raw` composes `int(whole) * 10**30 + int(frac.ljust(30,"0"))` and
  caps the fraction at 30 places - no `Decimal`, therefore none of the process-global `prec`
  trap that `langchain-vend` (#5), `gpt-researcher-x402-retriever` (#8),
  `openai-agents-nano-x402` (#20) and `nano-mcp-public` each carried. `raw_to_xno` is integer
  `divmod` and string work. `grep` finds exactly one `float(` in the package and it is the name
  of the function that refuses one.
- **Two reason codes for one condition, which turned out to be deliberate.**
  `cli check` answers `bad_checksum` while `selfcheck` asserts `invalid_checksum`.
  `payments.py:29` holds an explicit `{"bad_checksum": "invalid_checksum"}` translation between
  the `nanoaddr` layer and the payments/MCP layer, and both codes are pinned by tests on their own
  side. A mapping, not a drift.
- **Secrets, in the tree and in `git log --all -p`.** None. No seed or private-key literal appears
  in any added line in history. Keys are generated in-process and the README's non-custodial claim
  is enforced by `test_nothing_in_the_money_path_imports_a_network_module` rather than by prose.

## Found

Nothing worth a patch this run.

## Could not verify

- **Anything against a real Nano node.** This environment's network policy answers 403 to CONNECT
  for the public RPC hosts, so `balance` and `receive` are exercised only against `fakenode.py`
  and the recorded shapes in the tests. No XNO moved.
- **The README's "Python 3.8+" floor.** Only 3.11, 3.12 and 3.13 are installed here. The install and
  the suite were run on 3.11; 3.8 through 3.10 are unexecuted.

## Left alone on purpose

`#4` (`account_info` paired the tip's frontier with the confirmed balance) and `#13`
(precompute a send's work) are both open, both clean and mergeable, and both move an amount or the
send path beyond "only adds a refusal" - so both stay the owner's decision under TIER0's merge rule.
Per run 127 neither was re-commented: each already carries a full verification row in
`NEEDS-OWNER.md`, and a third "still waiting" comment adds nothing a reader needs.
