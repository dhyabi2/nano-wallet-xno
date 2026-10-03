# nano-wallet-xno - audit 2026-10-03

Worked the repository's two open pull requests, #5 and #4, both left open by their
authors because both sit inside the code that moves XNO. One qualifies for merge under
the owner's 2026-10-03 rule and one does not; the distinction is the whole of this note.

## Checked

- `python3 -m unittest discover -s tests`, `e2e_check.py`, `e2e_receive_only.py`,
  `selfcheck.py` and `py_compile` on `main` and on each branch merged with `main`.
  `main`: 101 tests. #5's tree: **102 passed, 15/15, 16/16, 7/7, compile clean**.
  #4's tree: **107 passed, 15/15, 16/16, 7/7, compile clean**. Neither branch needed a
  merge commit - `git merge origin/main` reports "Already up to date" on both - so the
  tested trees are the trees that land.
- #5 failing-then-passing: with `main`'s `payments.py` restored under #5's tests, all four
  subtests of
  `test_a_misconfigured_send_limit_is_named_and_not_blamed_on_the_amount`
  error out of `wallet.py` (`ValueError: amount is not a decimal number`); with the branch's
  `payments.py` they pass.
- Whether #5 is a refusal and nothing else, which is what decides if it may be merged from
  here. It is: the diff wraps one existing call in `try`/`except ValueError` and raises
  `ToolError("misconfigured_send_limit", ..., 403)`. No amount, destination, rounding or
  key path is touched, nothing is signed or published on the new path, and a well-formed
  `NANO_WALLET_MAX_SEND_XNO` reaches exactly the code it reached before. Both the old and
  the new behaviour refuse the send; the change is which error the agent is handed, and the
  old one named a value the agent never sent.
- #4's arithmetic, independently of its description, by reading `main`:
  `nanonode.py:109-121` returns `answer["frontier"]` (the **tip**) beside
  `int(answer.get("confirmed_balance", answer["balance"]))` (the **confirmation height**)
  and `answer.get("confirmed_representative", ...)`. `payments.send`
  (`payments.py:286-289`) then uses them as a pair - `previous = bytes.fromhex(info["frontier"])`
  and `balance_raw - amount_raw` as the new balance - and Nano reads a send's amount as
  `previous.balance - block.balance`. So the amount that actually leaves is
  `amount + (tip_balance - confirmed_balance)`. The defect is real and it is on `main` today.
- Whether #4's change to the `confirmed` flag can reach a money decision, since it now
  answers `True` where `main` answered `False` for a node that reports no confirmed
  frontier. It cannot: `confirmed` is read in exactly one place,
  `payments.py:148`, and only to be reported back out of the `balance` read-out. Nothing
  gates a send or a receive on it.

## Found

- **#5's README count.** The suite moves 101 -> 102 and `README.md:245` still published
  `Ran 101 tests - OK`. Corrected in this merge. (No law in this repository reads that
  number, which is why nothing failed - `bounded-task-spec` and `nano-mcp-public` both have
  one and both caught the equivalent slip.)
- **#4 advertises a balance it no longer returns.** `mcp_server.py:73` describes the
  `balance` tool as "Read an account's **confirmed** balance and what is waiting to be
  pocketed." That was true of `main`, which returned `confirmed_balance`. #4 correctly
  switches to the tip's balance, which makes the tool's own description wrong for an agent
  reading it to decide what it may spend. Raised on #4 rather than pushed, because #4 is
  the owner's call and this is a line in its diff's blast radius, not a separate defect.

## Fixed

- `README.md:245`, 101 -> 102, in the same merge as #5.

Nothing else. #4 is not merged from here: see below.

## Could not verify

- **#4 is not merged and must not be, from here.** It is the send path and it is not a
  refusal - it changes which balance the send amount is computed from, which is precisely
  what the owner's rule reserves. Its three candidate designs (build on the tip; build on
  the confirmed state; refuse to send while `frontier != confirmed_frontier`) are a
  judgement about a wallet that spends real XNO, and the branch argues for the first. The
  arithmetic is verified here; the choice is not mine.
- No live node was reached. #4's field names (`confirmed_balance`, `confirmed_frontier`,
  `confirmation_height_frontier`, `confirmed_representative`) are read from the node RPC's
  documented response, not captured from a running node, and both branches' tests drive
  `HttpNanoNode` with `_rpc` replaced by a recorded answer. The arithmetic consequence does
  not depend on the names, but the names themselves are unconfirmed.
- The `nanolooker.com/block/<hash>` receipt URL that `send` returns on every payment: the
  container's network policy denies it, so whether it resolves is unverified.
