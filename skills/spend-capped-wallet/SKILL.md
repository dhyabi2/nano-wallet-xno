---
name: spend-capped-wallet
description: A Nano (XNO) wallet the agent holds itself, with a per-payment cap and a per-day cap enforced in code. The key stays in a local file; the agent's tools take no cap parameter.
---

# Spend-capped wallet (self-held)

Use this when an agent should be able to pay for a service in XNO without a human approving each payment, and without
being able to spend more than a small amount a day. Standard library only; no network call until you give it a node.

## Set it up once (the human or the framework, not the agent)

```
git clone https://github.com/dhyabi2/nano-wallet-xno && cd nano-wallet-xno
python3 capped_wallet.py --home ~/.nano-capped --per-call 0.01 --per-day 0.05 init     # offline; prints the address only
```

Fund that address with no more than the day cap. The caps are saved in `~/.nano-capped/caps.json`; a later run can lower
them but a larger number passed by mistake is ignored (the lower of the two wins).

## Wire the tools into a framework

```python
import capped_wallet
from nanonode import HttpNanoNode

wallet = capped_wallet.CappedWallet("~/.nano-capped", node=HttpNanoNode("https://<your node rpc>"))
for tool in wallet.tools():          # name, description, JSON-Schema parameters, fn(**kwargs) -> {"ok": ..., ...}
    register(tool["name"], tool["description"], tool["parameters"], tool["fn"])   # your framework's registration call
```

The four tools: `wallet_address`, `wallet_status` (what is left today), `wallet_balance`, `wallet_pay(payee, amount_xno,
idempotency_key)`. A refused payment returns `{"ok": false, "reason": ...}` (`over_per_call_cap`, `over_daily_cap`,
`ledger_unreadable`, ...) and nothing is signed. The same idempotency key never pays twice.

## Check a payment without sending anything

```
python3 capped_wallet.py --home ~/.nano-capped pay nano_<payee> 0.005 --key demo-1       # dry run: the cap check only
```

`--send` is required to really send, and needs `NANO_NODE_URL`.

## What it does not protect against

Anyone with shell access to the home directory can delete the ledger or read the key. The caps stop an agent that only has
the tools (including a prompt-injected one); they do not stop the machine's owner. Keep the balance at or below the day cap.
