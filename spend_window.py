"""A per-purpose account whose balance IS the allowance: the record an operator reads and a stranger checks.

Design (specs/agent-tool-spend-window-account in swarm-decisions; atlastr_oz, 2026-10-25: Nano's per-spend signature
"forces users to sign on every transaction"; 13 agents say a human must approve each spend, 16 that they cannot
spend autonomously). Nano has no permit, no approve, no cap a counterparty can be granted once. An account balance
can express one: fund a purpose account with exactly the approved amount and nothing more, and the agent cannot
exceed the cap because there is nothing above the cap to spend. Revoking is one sweep of the remainder back to the
funder; there is no cached approval to revoke.

This module is only the RECORD of that arrangement:
  open    what the account is for, the ceiling, who approved it once and where they said so, when it ends
  record  a send that ALREADY happened (block, amount, payee); the running total is integer arithmetic over raw
  close   exhausted | expired | revoked (revoked needs the sweep block: a fact on a public ledger, not a flag)
  feed    a byte-reproducible summary

It has no key path, never sends, never sweeps and imports nothing that can open a socket. Caps are enforced in
code at record time: a send that would take cumulative spend past the ceiling is refused loudly (it means the
account held more than its ceiling, so the premise of the record is broken), as is a payee outside the optional
allow-list and a send above the optional per-call cap. Any real send, sweep or funding stays with the operator:
see docs/SPEND-WINDOW-ACCOUNT.md. 1 XNO = 10**30 raw; floats are refused everywhere.

    python3 spend_window.py open --purpose "..." --ceiling-raw 50000000000000000000000000000 --address nano_... \\
        --approved-by "ops@example" --approved-at 2026-10-25T05:00:00Z --approval-url https://... \\
        --expires 2026-11-25T00:00:00Z --now 2026-10-25T06:00:00Z
"""
import argparse
import json
import re
import sys

import nanoaddr

STATE_FILE = "spend_windows.json"
V = "spend-window-v1"
XNO = 10 ** 30
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HASH = re.compile(r"^[0-9A-Fa-f]{64}$")
NOTE = ("This account is funded to its ceiling and no further. Nothing in this record can authorise a send above "
        "the balance, because nothing above the balance exists. Revocation is one sweep of the remainder to the "
        "funder; there is no cached approval to revoke.")


class WindowError(Exception):
    def __init__(self, code, message=""):
        super().__init__("%s: %s" % (code, message) if message else code)
        self.code, self.message = code, message


def _raw(value, code="ceiling_not_integer_string"):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise WindowError(code, "raw amounts are integers; got %r" % (value,))
    text = str(value)
    if not (text.isascii() and text.isdigit()):
        raise WindowError(code, "raw amounts are digit strings of integer raw (1 XNO = 10**30), got %r" % (text,))
    return int(text)


def _xno(raw):
    whole, frac = divmod(raw, XNO)
    return str(whole) if not frac else "%d.%s" % (whole, ("%030d" % frac).rstrip("0"))


def _time(text, name, code="no_clock"):
    if not isinstance(text, str) or not _ISO.match(text):
        raise WindowError(code, "%s must be UTC ISO-8601 like 2026-10-25T06:00:00Z" % name)
    return text  # ISO-8601 UTC strings of one shape sort chronologically


def load(path=STATE_FILE):
    try:
        with open(path) as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return {"spend_windows": []}
    if not isinstance(state, dict) or not isinstance(state.get("spend_windows"), list):
        raise WindowError("bad_state", "%s is not a spend_windows file" % path)
    return state


def dump(state):
    return json.dumps(state, indent=2, sort_keys=True) + "\n"


def save(state, path=STATE_FILE):
    with open(path, "w") as fh:
        fh.write(dump(state))


def _view(w):
    spent = sum(int(s["amount_raw"]) for s in w["sends"])
    ceiling = int(w["ceiling_raw"])
    out = dict(w)
    out.update(spent_raw=str(spent), spent_xno=_xno(spent), remaining_raw=str(ceiling - spent),
               remaining_xno=_xno(ceiling - spent), ceiling_xno=_xno(ceiling))
    return out


def open_window(state, *, purpose, ceiling_raw, address, approved_by, approved_at, approval_url, expires, now,
                allowed_payees=None, per_call_cap_raw=None):
    now = _time(now, "--now")
    if not purpose or not approved_by:
        raise WindowError("missing_field", "purpose and approved_by are required")
    ceiling = _raw(ceiling_raw)
    if ceiling <= 0:
        raise WindowError("ceiling_not_integer_string", "ceiling must be positive")
    if not nanoaddr.is_valid(address):
        raise WindowError("bad_address_checksum", "the window address fails its checksum: %s" % address)
    if not (isinstance(approval_url, str) and re.match(r"^https?://[^\s/]+\S*$", approval_url)):
        raise WindowError("approval_not_citable", "--approval-url must be an absolute http(s) URL anyone can read")
    if not expires:
        raise WindowError("no_expiry", "an allowance with no end is not an allowance")
    _time(approved_at, "--approved-at", "missing_field")
    _time(expires, "--expires", "no_expiry")
    if expires <= now:
        raise WindowError("expiry_not_after_now", "--expires %s is not after --now %s" % (expires, now))
    payees = sorted(set(allowed_payees or []))
    for p in payees:
        if not nanoaddr.is_valid(p):
            raise WindowError("bad_address_checksum", "allowed payee fails its checksum: %s" % p)
    cap = None if per_call_cap_raw is None else _raw(per_call_cap_raw, "per_call_cap_not_integer_string")
    if cap is not None and not 0 < cap <= ceiling:
        raise WindowError("per_call_cap_not_integer_string", "per-call cap must be within 1..ceiling")
    day = now[:10]
    n = 1 + sum(1 for w in state["spend_windows"] if w["id"].startswith("sw-%s-" % day))
    w = {"v": V, "id": "sw-%s-%03d" % (day, n), "state": "open", "purpose": purpose, "address": address,
         "ceiling_raw": str(ceiling), "approved_by": approved_by, "approved_at": approved_at,
         "approval_url": approval_url, "expires": expires, "opened_at": now, "sends": [],
         "allowed_payees": payees, "per_call_cap_raw": None if cap is None else str(cap),
         "the_allowance_is_the_balance": True, "note": NOTE}
    state["spend_windows"].append(w)
    return _view(w)


def _find(state, wid):
    for w in state["spend_windows"]:
        if w["id"] == wid:
            return w
    raise WindowError("unknown_window", wid)


def record_send(state, wid, *, block, amount_raw, payee, now):
    now = _time(now, "--now")
    w = _find(state, wid)
    if w["state"] != "open":
        raise WindowError("window_closed", "%s is %s" % (wid, w["state"]))
    amount = _raw(amount_raw, "amount_not_integer_string")
    if amount <= 0:
        raise WindowError("amount_not_integer_string", "amount must be positive")
    if not _HASH.match(str(block)):
        raise WindowError("bad_block_hash", "block must be 64 hex characters")
    if not nanoaddr.is_valid(payee):
        raise WindowError("bad_address_checksum", "payee fails its checksum: %s" % payee)
    block = block.upper()
    if any(s["block"] == block for s in w["sends"]):
        raise WindowError("block_already_recorded", block)
    if w["allowed_payees"] and payee not in w["allowed_payees"]:
        raise WindowError("payee_not_allowed", "%s is outside this window's allow-list" % payee)
    if w["per_call_cap_raw"] is not None and amount > int(w["per_call_cap_raw"]):
        raise WindowError("above_per_call_cap", "%d raw is above the per-call cap %s" % (amount, w["per_call_cap_raw"]))
    spent = sum(int(s["amount_raw"]) for s in w["sends"])
    if spent + amount > int(w["ceiling_raw"]):
        raise WindowError("would_exceed_ceiling", "cumulative %d raw would pass the ceiling %s raw: the account held "
                          "more than its ceiling, so the premise of this record is broken" % (spent + amount, w["ceiling_raw"]))
    w["sends"].append({"block": block, "amount_raw": str(amount), "payee": payee, "recorded_at": now})
    return _view(w)


def close_window(state, wid, *, reason, now, sweep_block=None):
    now = _time(now, "--now")
    w = _find(state, wid)
    if w["state"] != "open":
        raise WindowError("window_closed", "%s is already %s" % (wid, w["state"]))
    if reason not in ("exhausted", "expired", "revoked"):
        raise WindowError("bad_reason", "reason is exhausted, expired or revoked")
    if reason == "revoked" and not (sweep_block and _HASH.match(sweep_block)):
        raise WindowError("revocation_without_sweep", "revocation is a fact on a public ledger: give --sweep-block")
    view = _view(w)
    if reason == "exhausted" and view["remaining_raw"] != "0":
        raise WindowError("not_exhausted", "%s raw remain" % view["remaining_raw"])
    if reason == "expired" and now < w["expires"]:
        raise WindowError("not_expired", "expires %s, now %s" % (w["expires"], now))
    w["state"], w["closed_reason"], w["closed_at"] = "closed", reason, now
    if sweep_block:
        w["sweep_block"] = sweep_block.upper()
    return _view(w)


def feed(state, now):
    now = _time(now, "--now")
    rows = [_view(w) for w in state["spend_windows"]]
    live = [w for w in rows if w["state"] == "open" and w["expires"] > now]
    lapsed = [w for w in rows if w["state"] == "open" and w["expires"] <= now]
    ceiling = sum(int(w["ceiling_raw"]) for w in rows)
    spent = sum(int(w["spent_raw"]) for w in rows)
    return {"v": V + "/feed", "generated_for": now, "open_windows": len(live), "open_windows_as_of": now,
            "lapsed_but_not_closed": len(lapsed), "lapsed_but_not_closed_as_of": now,
            "ceiling_total_raw": str(ceiling), "ceiling_total_xno": _xno(ceiling),
            "spent_total_raw": str(spent), "spent_total_xno": _xno(spent),
            "windows_revoked": sum(1 for w in rows if w.get("closed_reason") == "revoked"),
            "cached_approvals_outstanding": 0,
            "cached_approvals_note": "always 0: Nano has no approval to cache. The allowance is the balance, so "
                                     "there is nothing to leave outstanding and revocation is a sweep.",
            "windows": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spend_window", description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", default=STATE_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    for f in ("purpose", "ceiling-raw", "address", "approved-by", "approved-at", "approval-url", "expires", "now"):
        o.add_argument("--" + f)
    o.add_argument("--allowed-payee", action="append")
    o.add_argument("--per-call-cap-raw")
    r = sub.add_parser("record")
    r.add_argument("id")
    for f in ("block", "amount-raw", "payee", "now"):
        r.add_argument("--" + f)
    c = sub.add_parser("close")
    c.add_argument("id")
    for f in ("reason", "sweep-block", "now"):
        c.add_argument("--" + f)
    fd = sub.add_parser("feed")
    fd.add_argument("--now")
    fd.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        state = load(a.state)
        if a.cmd == "open":
            out = open_window(state, purpose=a.purpose, ceiling_raw=a.ceiling_raw, address=a.address,
                              approved_by=a.approved_by, approved_at=a.approved_at, approval_url=a.approval_url,
                              expires=a.expires, now=a.now, allowed_payees=a.allowed_payee,
                              per_call_cap_raw=a.per_call_cap_raw)
        elif a.cmd == "record":
            out = record_send(state, a.id, block=a.block, amount_raw=a.amount_raw, payee=a.payee, now=a.now)
        elif a.cmd == "close":
            out = close_window(state, a.id, reason=a.reason, now=a.now, sweep_block=a.sweep_block)
        else:
            text = dump(feed(state, a.now))
            if a.out:
                with open(a.out, "w") as fh:
                    fh.write(text)
            else:
                sys.stdout.write(text)
            return 0
        save(state, a.state)  # only after every check passed: a refusal leaves the file byte-identical
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    except WindowError as e:
        print(json.dumps({"error": e.code, "message": e.message}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
