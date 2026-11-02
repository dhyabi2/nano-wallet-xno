#!/usr/bin/env python3
"""tomb_check.py - is a funded Nano address still being used, or is it a tomb? Read-only; no key, sends nothing.

Raised by x402wrapper: an onramp that hands a seed back once and stores nothing leaves a funded address whose only copy of
the key is an agent runtime; if that runtime is ephemeral, the address is dead on restart, "a tomb the network can't
distinguish from a live envelope". The chain can show the one observable that separates them from the outside: how long since
the account last changed, while it still holds funds.

  python3 tomb_check.py nano_ADDRESS [--idle-days 14] [--rpc URL,URL]
Verdicts: unopened (nothing there), receivable_unopened (funds waiting, never opened), live (changed within --idle-days),
          tomb_suspect (holds funds, no change for longer than --idle-days), empty (opened, balance 0).
Exit: 0 live/empty/unopened, 3 tomb_suspect or receivable_unopened, 4 no node answered, 64 usage.
Limit, printed in the output: a long-idle funded account may be a deliberate vault; the chain cannot say. The check is
a prompt to look at the runtime, not proof of loss. Fix at the source: see docs/EPHEMERAL-RUNTIMES.md.
"""
import argparse, json, sys, time, urllib.request, urllib.error

import nanoaddr as _addr

RPCS = ["https://rainstorm.city/api", "https://rpc.nano.to", "https://node.somenano.com/proxy"]
XNO = 10 ** 30
LIMIT = "idle does not prove loss: a deliberate vault looks the same on chain; check where the seed lives"


def _post(rpc, payload):
    req = urllib.request.Request(rpc, json.dumps(payload).encode(), {"Content-Type": "application/json", "User-Agent": "nano-tomb-check"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def check(address, idle_days=14, rpcs=RPCS, post=_post, now=None):
    now = now if now is not None else time.time()
    info = recv = None
    for rpc in rpcs:
        try:
            info = post(rpc, {"action": "account_info", "account": address})
            recv = post(rpc, {"action": "receivable", "account": address, "threshold": "1", "count": "1", "source": "true"})
            break
        except (urllib.error.URLError, OSError, ValueError):
            continue
    if info is None:
        return 4, {"address": address, "verdict": "no_node_answered"}
    waiting = recv.get("blocks") if isinstance(recv, dict) else None
    has_recv = bool(waiting) and waiting != ""
    out = {"address": address, "limit": LIMIT}
    if info.get("error") == "Account not found":
        out["verdict"] = "receivable_unopened" if has_recv else "unopened"
        return (3 if has_recv else 0), out
    if "error" in info:
        out["verdict"] = "node_error"; out["detail"] = info["error"]; return 4, out
    bal = int(info.get("balance", "0")); mod = int(info.get("modified_timestamp", "0"))
    age = (now - mod) / 86400 if mod else None
    out.update(balance_xno=f"{bal // XNO}.{(bal % XNO) // 10**18:012d}", last_change_days=round(age, 2) if age is not None else None,
               receivable_waiting=has_recv, block_count=info.get("block_count"))
    if bal == 0 and not has_recv:
        out["verdict"] = "empty"; return 0, out
    if age is not None and age > idle_days:
        out["verdict"] = "tomb_suspect"; return 3, out
    out["verdict"] = "live"; return 0, out


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("address"); ap.add_argument("--idle-days", type=float, default=14); ap.add_argument("--rpc")
    a = ap.parse_args(argv)
    if not _addr.is_valid(a.address): print("bad address", file=sys.stderr); return 64
    code, out = check(a.address, a.idle_days, a.rpc.split(",") if a.rpc else RPCS)
    print(json.dumps(out)); return code


if __name__ == "__main__":
    sys.exit(main())
