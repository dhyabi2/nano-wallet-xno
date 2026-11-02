"""A spend-capped wallet an agent holds itself, for agent frameworks.

The gap this closes (outside agents, Moltbook 2026-10/11): "the thing that turns 0 into N is exactly one artifact: an
agent framework that hands a conversational agent a spend-capped wallet it controls" and "permission to seek revenue is
not the same as holding a funded wallet authorized to buy services". `mandate.py` answers the operator-signed version of
that. This is the self-held version: no operator signature, no account, no network at construction. The framework (or
the human who installs the skill) picks two numbers once; the agent can then pay, and cannot pay more than them.

    wallet = CappedWallet("~/.nano-capped", per_call_max_xno="0.01", per_day_max_xno="0.05", node=HttpNanoNode(url))
    wallet.create()                         # offline: key in <home>/key.json, mode 600, never overwritten, never returned
    wallet.address()                        # the only thing to hand out; fund it with a few cents, not more than the day cap
    wallet.pay("nano_...", "0.001", "order-17")   # refused in code above either cap, before anything is signed

What is enforced, and where
  per call   one payment may not exceed per_call_max (also handed to payments.send as NANO_WALLET_MAX_SEND_XNO, so it is
             checked twice, once here and once inside the signer)
  per day    the sum of every payment started in the trailing 24 hours (a rolling window, stricter than a calendar day)
             plus this one may not exceed per_day_max. Reserved in the ledger BEFORE the block is broadcast; a send that
             raised stays counted, because Nano has no reversal and a lost reply may have landed.
  fail closed  an unreadable or foreign ledger, a float amount, a payee that fails its checksum, a clock that went
             backwards: all refuse. Nothing is signed.
  tightening only  the caps are saved in <home>/caps.json at create(); a later construction uses the LOWER of what it is
             given and what is saved, so a larger number passed by mistake (or by a prompt) cannot loosen a wallet.
  the tools the agent sees (tools()) take no cap parameter at all; there is nothing to argue with.

What it does NOT do (say this to the operator): anyone with shell access to <home> can delete the ledger or caps.json, or
read key.json. The files stop an honest runtime and a prompt-injected agent that only has the tools; they do not stop the
machine's owner. For a hard ceiling, also fund the address with no more than the day cap. Both together are the story.

Standard library only. Money is integer raw (1 XNO = 10**30) end to end; floats are refused.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import keystore as _keystore
import mandate as _mandate
import payments
import wallet as _wallet

DAY_SECONDS = 24 * 3600
DEFAULT_PER_CALL_XNO = "0.01"
DEFAULT_PER_DAY_XNO = "0.05"
Refused = _mandate.MandateRefused          # one refusal type, so payments.send maps it to a 403 ToolError


def _raw(value, name):
    if isinstance(value, float) or isinstance(value, bool):
        raise Refused("float_amount", "%s must be a decimal string like \"0.01\", not a float" % name)
    try:
        raw = _wallet.xno_to_raw(str(value))
    except ValueError as exc:
        raise Refused("invalid_amount", "%s: %s" % (name, exc)) from None
    if raw <= 0:
        raise Refused("invalid_amount", "%s must be greater than zero" % name)
    return raw


class SpendCap:
    """Per-call and rolling per-day cap with a local ledger. Same duck type as mandate.MandateGuard (check / spend)."""

    def __init__(self, ledger_path, per_call_max_raw, per_day_max_raw, clock=None):
        self.ledger_path = ledger_path
        self.per_call_max_raw = int(per_call_max_raw)
        self.per_day_max_raw = int(per_day_max_raw)
        self.clock = clock or time.time
        if self.per_call_max_raw <= 0 or self.per_day_max_raw <= 0:
            raise Refused("bad_caps", "both caps must be greater than zero")
        if self.per_call_max_raw > self.per_day_max_raw:
            raise Refused("bad_caps", "the per-call cap cannot exceed the per-day cap")

    # -- ledger ------------------------------------------------------------
    def _read(self):
        if not os.path.exists(self.ledger_path):
            return {"v": 1, "payments": []}
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("v") != 1 or not isinstance(data.get("payments"), list):
                raise ValueError("unknown ledger shape")
            for p in data["payments"]:
                int(p["amount_raw"]); float(p["at"])
            return data
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Refused("ledger_unreadable", "cannot read ledger %s (%s); refusing to spend" % (self.ledger_path, exc)) from None

    def _write(self, data):
        directory = os.path.dirname(os.path.abspath(self.ledger_path))
        fd, tmp = tempfile.mkstemp(prefix=".cap-ledger-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.ledger_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _window(self, data, now):
        last = max([float(p["at"]) for p in data["payments"]] or [0.0])
        if now < last:
            raise Refused("clock_went_backwards", "the clock is earlier than the last recorded payment; refusing to spend")
        return sum(int(p["amount_raw"]) for p in data["payments"] if float(p["at"]) > now - DAY_SECONDS)

    def _check(self, data, now, amount_raw):
        if amount_raw > self.per_call_max_raw:
            raise Refused("over_per_call_cap", "%s raw is above the per-call cap of %s raw" % (amount_raw, self.per_call_max_raw))
        used = self._window(data, now)
        if used + amount_raw > self.per_day_max_raw:
            raise Refused("over_daily_cap", "%s raw requested, %s raw left of the %s raw daily cap (trailing 24 h)"
                          % (amount_raw, max(self.per_day_max_raw - used, 0), self.per_day_max_raw))
        return used

    # -- the MandateGuard-shaped interface payments.send calls ---------------
    def check(self, payee, amount):
        amount_raw = int(amount)
        _mandate.public_key_from_address(payee)            # a payee that fails its checksum is refused
        data = self._read()
        used = self._check(data, self.clock(), amount_raw)
        return {"ok": True, "amount_raw": str(amount_raw), "remaining_after_raw": str(self.per_day_max_raw - used - amount_raw)}

    def spend(self, payee, amount, send, ref=None):
        amount_raw = int(amount)
        _mandate.public_key_from_address(payee)
        with _mandate._Locked(self.ledger_path):
            data = self._read()
            now = self.clock()
            self._check(data, now, amount_raw)
            data["payments"].append({"at": repr(float(now)), "amount_raw": str(amount_raw),
                                     "payee": _mandate.normalise_address(payee), "ref": ref or "", "status": "pending"})
            self._write(data)
            index = len(data["payments"]) - 1
            try:
                result = send()
            except BaseException:
                data["payments"][index]["status"] = "unknown"      # may have landed: stays counted
                self._write(data)
                raise
            data["payments"][index]["status"] = "returned"
            self._write(data)
        return result

    def status(self):
        with _mandate._Locked(self.ledger_path):
            data = self._read()
        used = self._window(data, self.clock())
        left = max(self.per_day_max_raw - used, 0)
        return {"per_call_max_xno": _wallet.raw_to_xno(self.per_call_max_raw), "per_day_max_xno": _wallet.raw_to_xno(self.per_day_max_raw),
                "spent_last_24h_xno": _wallet.raw_to_xno(used), "left_today_xno": _wallet.raw_to_xno(left),
                "payments_recorded": len(data["payments"])}


class CappedWallet:
    def __init__(self, home, per_call_max_xno=DEFAULT_PER_CALL_XNO, per_day_max_xno=DEFAULT_PER_DAY_XNO, node=None,
                 clock=None, keys=None):
        self.home = os.path.abspath(os.path.expanduser(home))
        self.node = node
        self.keys = keys or _keystore.KeyStore()
        self.key_path = os.path.join(self.home, "key.json")
        self.caps_path = os.path.join(self.home, "caps.json")
        self.sent_path = os.path.join(self.home, "sent.json")
        call, day = _raw(per_call_max_xno, "per_call_max_xno"), _raw(per_day_max_xno, "per_day_max_xno")
        saved = self._saved_caps()
        if saved:                                            # tightening only
            call, day = min(call, saved[0]), min(day, saved[1])
        self._caps = (call, day)
        self.cap = SpendCap(os.path.join(self.home, "ledger.json"), call, day, clock=clock)
        self.sent = self._load_sent()
        if os.path.exists(self.key_path):
            self.keys.load_file(self.key_path)

    # -- setup -------------------------------------------------------------
    def _saved_caps(self):
        try:
            with open(self.caps_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return int(d["per_call_max_raw"]), int(d["per_day_max_raw"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Refused("caps_unreadable", "cannot read %s (%s); refusing to run uncapped" % (self.caps_path, exc)) from None

    def _load_sent(self):
        try:
            with open(self.sent_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise Refused("sent_unreadable", "cannot read %s (%s); refusing to send" % (self.sent_path, exc)) from None

    def _save_sent(self):
        fd, tmp = tempfile.mkstemp(prefix=".sent-", dir=self.home)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.sent, fh, sort_keys=True)
            fh.flush(); os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.sent_path)

    def create(self):
        """Offline. Writes key.json (mode 600, never overwrites) and caps.json. Returns the address only."""
        if os.path.exists(self.key_path):
            raise Refused("wallet_exists", "a key already exists at %s; refusing to replace it" % self.key_path)
        out = payments.create_address(label="capped", store="file", path=self.key_path, keys=self.keys)
        with open(self.caps_path, "w", encoding="utf-8") as fh:
            json.dump({"per_call_max_raw": str(self._caps[0]), "per_day_max_raw": str(self._caps[1])}, fh)
        os.chmod(self.caps_path, 0o600)
        return {"address": out["address"], "stored": out["stored"], **self.status()}

    # -- the four things an agent can do -------------------------------------
    def address(self):
        a = self.keys.addresses()
        if not a:
            raise Refused("no_wallet", "no key at %s; call create() first" % self.key_path)
        return a[0]

    def balance(self):
        return payments.balance(self.address(), self._node())

    def receive(self):
        return payments.receive(self.address(), self._node(), self.keys)

    def status(self):
        s = self.cap.status()
        s["caps_file"] = self.caps_path
        return s

    def check(self, payee, amount_xno):
        """Dry run of the caps. Records nothing."""
        return self.cap.check(payee, _raw(amount_xno, "amount_xno"))

    def pay(self, payee, amount_xno, idempotency_key):
        amount_raw = _raw(amount_xno, "amount_xno")
        env = {"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_MAX_SEND_XNO": _wallet.raw_to_xno(self._caps[0])}
        try:
            result = payments.send(self.address(), payee, amount_xno, str(idempotency_key), self._node(), self.keys,
                                   self.sent, env=env, guard=self.cap)
        finally:
            self._save_sent()
        return result

    def _node(self):
        if self.node is None:
            raise Refused("node_not_configured", "no node: pass node=HttpNanoNode(url) (this wallet makes no network call otherwise)")
        return self.node

    # -- framework-neutral tool specs ----------------------------------------
    def tools(self):
        """Plain dicts any framework can wrap (OpenAI/Anthropic function schemas, LangChain StructuredTool, MCP, Hermes).
        No tool takes a cap, a key path, or a node URL."""
        def guarded(fn):
            def run(**kw):
                try:
                    return {"ok": True, "result": fn(**kw)}
                except Refused as exc:
                    return {"ok": False, **exc.as_dict()}
                except payments.ToolError as exc:
                    return {"ok": False, "reason": exc.reason, "message": exc.message}
            return run
        obj = lambda props, req: {"type": "object", "properties": props, "required": req, "additionalProperties": False}
        s = {"type": "string"}
        return [
            {"name": "wallet_address", "description": "Your own Nano (XNO) address. Share it to be paid.", "parameters": obj({}, []),
             "fn": guarded(lambda: {"address": self.address()})},
            {"name": "wallet_status", "description": "How much you may still spend today and per payment. Read-only.", "parameters": obj({}, []),
             "fn": guarded(lambda: self.status())},
            {"name": "wallet_balance", "description": "Your balance and pending XNO. Read-only.", "parameters": obj({}, []),
             "fn": guarded(lambda: self.balance())},
            {"name": "wallet_pay", "description": "Pay XNO from your own wallet. Refused above the per-payment or the daily cap; "
             "the same idempotency_key never pays twice. amount_xno is a decimal string.",
             "parameters": obj({"payee": s, "amount_xno": s, "idempotency_key": s}, ["payee", "amount_xno", "idempotency_key"]),
             "fn": guarded(lambda payee, amount_xno, idempotency_key: self.pay(payee, amount_xno, idempotency_key))},
        ]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="capped_wallet", description="A spend-capped, self-held Nano wallet. pay is a dry run unless --send.")
    ap.add_argument("--home", default=os.environ.get("NANO_CAPPED_HOME", "~/.nano-capped"))
    ap.add_argument("--per-call", default=DEFAULT_PER_CALL_XNO)
    ap.add_argument("--per-day", default=DEFAULT_PER_DAY_XNO)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("init", "address", "status", "balance"):
        sub.add_parser(c)
    p = sub.add_parser("pay")
    p.add_argument("payee"); p.add_argument("amount_xno"); p.add_argument("--key", required=True)
    p.add_argument("--send", action="store_true", help="really send (needs NANO_NODE_URL)")
    a = ap.parse_args(argv)
    node = None
    if os.environ.get("NANO_NODE_URL") and a.cmd in ("balance", "pay"):
        import nanonode
        node = nanonode.HttpNanoNode(os.environ["NANO_NODE_URL"])
    try:
        w = CappedWallet(a.home, a.per_call, a.per_day, node=node)
        if a.cmd == "init":
            out = w.create()
        elif a.cmd == "address":
            out = {"address": w.address()}
        elif a.cmd == "status":
            out = w.status()
        elif a.cmd == "balance":
            out = w.balance()
        else:
            out = w.pay(a.payee, a.amount_xno, a.key) if a.send else dict(w.check(a.payee, a.amount_xno), dry_run=True)
    except (Refused, payments.ToolError, _keystore.KeyStoreError) as exc:
        print(json.dumps({"ok": False, "reason": getattr(exc, "reason", "error"), "message": getattr(exc, "message", str(exc))}))
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
