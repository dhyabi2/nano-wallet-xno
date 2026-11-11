# Send-side work without a node: options and a prototype (2026-11-02)

Status: PROTOTYPE on branch `feat/work-service-proto`, not wired into the wallet, spends nothing, needs the owner to
merge (the send-work path is money-adjacent; see `work.py`). Receive work and the pre-compute cache already shipped.

## What a send needs
`work.SEND_THRESHOLD = 0xfffffff800000000`: 2**29 expected hashes. Measured today on this Mac with `python3 cli.py work
estimate`: 823,709 hashes/s over 8 workers, so about 81.5 s on average (one search in twenty takes three times that).
Work depends on the account's frontier only (not amount, destination or key), which is why it can be done early, or bought.

## Options
| Option | Needs | Evidence | Verdict |
|---|---|---|---|
| Local pure-Python, ahead of time (`prework.py`) | cores + a background moment | shipped, 137 s on 4 cores per its own test note; 81.5 s on 8 workers today | works for any agent that can run a job before it pays |
| Client WebGPU/WebGL/WASM: npm `nano-pow` 5.2.4 | a browser or runtime with WebGPU/WebGL2, or its WASM fallback | package description: "Proof-of-work generation and validation with WebGPU/WebGL/WASM for Nano" (npm registry, published 2026-08-03). Licence `GPL-3.0-or-later AND MIT`: bundling it into this MIT repo needs a licence decision | the right tool where a browser or GPU exists. I did NOT run it (executing downloaded code was blocked here), so its speed is unmeasured by me |
| `nano-webgl-pow` 1.1.1 | WebGL2 | npm registry, MIT, last published 2020-10-24 | old; not checked against current thresholds |
| A node's `work_generate` | a node that allows it | this repo's `work.py`: public nodes refuse it | not dependable |
| Pay-per-work endpoint priced in XNO | a seller and a payment | `work_service.py`, below | for hosted agents that cannot run a background process |

It lives under `proto/work_service/` on purpose: the wallet's own law (`test_nothing_in_the_money_path_imports_a_network_module`) pins the set of network-touching top-level modules, and a new one is an owner decision, not something a prototype slips in.

## `work_service.py`
POST `/work {"hash": frontier, "threshold": "send"}`. Unpaid: HTTP 402 with a `nano:mainnet` `accepts[]` entry
(`maxAmountRequired` in raw, `payTo`). Retry with `X-PAYMENT` (same header shape as `pay402.py`) naming a confirmed send
block: the service verifies it through an injected `verify_payment`, claims the block (one payment, one work, stored in
SQLite), solves with `work.solve_parallel`, re-validates, and returns the 16-hex work. If solving fails the payment is NOT
consumed. Refusals: no price configured (there is no default price; operator policy), no `payTo`, any threshold other than
send/receive (test thresholds only with `allow_test`), bad hash, unverified payment, reused payment.

Client `get_work(...)` is refusal-only: without a `pay` callable it only returns the quote; it never pays above
`max_price_raw`; it re-checks the returned work with `work.validates` and refuses work that does not validate. The buyer
never has to trust the seller, because a bad answer costs it one payment at most and is detectable before use.

Tests: `proto/work_service/tests/test_work_service.py`, 15 cases, no node and no funds; the solver runs for real at a test-only threshold
(`0xffff000000000000`). Not tested: a real XNO payment, and the default payment verifier against a live node (the verifier
is injected; wiring it to `nanonode` is the remaining step). Nothing here moves funds.
