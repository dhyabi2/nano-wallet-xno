"""The five tools, as plain functions. The MCP server is a thin shell over this.

Money is an integer count of raw everywhere below (1 XNO = 10**30 raw).
Decimal XNO strings are parsed by `wallet.xno_to_raw` and rendered by
`wallet.raw_to_xno`, both exact. No float touches a balance, an amount or
a comparison in this file. A double holds 53 bits of mantissa and a raw
balance needs up to 128, so a single float round-trip would silently
round away real money.
"""

import os

import blocks
import keystore as _keystore
import mandate as _mandate
import nanoaddr as _addr
import nanonode
import profiles
import wallet

#: The spec's error table uses `invalid_*`; nanoaddr's parser has always used
#: `bad_*`. Map at the boundary rather than renaming the parser's codes, which
#: are pinned by known-answer tests, and expose BOTH so neither contract breaks.
CANONICAL_REASON = {
    "bad_prefix": "invalid_prefix",
    "bad_length": "invalid_length",
    "bad_character": "invalid_charset",
    "bad_padding": "invalid_padding",
    "bad_checksum": "invalid_checksum",
    "not_a_string": "invalid_prefix",
    "empty": "invalid_prefix",
}

CUSTODY_SENTENCE = (
    "This key was generated in your process. It was not transmitted. "
    "No third party can spend from this address."
)

UNPAYABLE_SENTENCE = (
    "This address would be rejected by every Nano node - a payment sent to it can "
    "never arrive, and nothing is lost in transit because nothing is ever sent. "
    "Regenerate with create_address."
)

DEFAULT_MAX_SEND_XNO = "1.0"
MAX_RECEIVE_BLOCKS = 64


class ToolError(Exception):
    """A tool refusal with the spec's code and HTTP-ish status."""

    def __init__(self, reason: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status
        self.extra = extra

    def payload(self) -> dict:
        out = {"error": self.reason, "status": self.status, "message": self.message}
        out.update(self.extra)
        return out


# ---------------------------------------------------------------- offline

def validate_address(address: str) -> dict:
    """Offline. Never raises; a rejection says what a payment to it would do."""
    verdict = _addr.validate(address)
    if verdict["valid"]:
        candidate = str(address).strip()
        out = {
            "valid": True,
            "canonical": verdict["normalised"],
            "public_key": verdict["public_key"],
        }
        if candidate.startswith("xrb_"):
            out["prefix_normalised_from"] = "xrb_"
        return out
    return {
        "valid": False,
        "reason": CANONICAL_REASON.get(verdict["reason"], verdict["reason"]),
        "parser_reason": verdict["reason"],
        "message": "%s %s" % (verdict["message"].rstrip("."), UNPAYABLE_SENTENCE),
        "position": None,
    }


def require_valid(address: str) -> bytes:
    """Validate before anything else, and before any network call."""
    verdict = validate_address(address)
    if not verdict["valid"]:
        raise ToolError("invalid_address", verdict["message"], 400,
                        reason_detail=verdict["reason"])
    return bytes.fromhex(verdict["public_key"])


def create_address(label: str = None, store: str = "memory", path: str = None,
                   keys: _keystore.KeyStore = None) -> dict:
    """Offline. The seed and the private key are NEVER in the return value."""
    if store not in ("memory", "file"):
        raise ToolError("invalid_store", "store must be \"memory\" or \"file\", got %r" % store)
    if label is not None and len(str(label)) > 64:
        raise ToolError("invalid_label", "label must be at most 64 characters")
    if store == "file" and not path:
        raise ToolError("invalid_path", "store=\"file\" requires a path")

    account = wallet.create_wallet()
    private_key = bytes.fromhex(account["private_key"])
    keys = keys if keys is not None else _keystore.KeyStore()
    keys.put(private_key)

    if store == "file":
        try:
            written = keys.save(private_key, path)
        except _keystore.KeyStoreError as exc:
            raise ToolError(exc.reason, exc.message, **exc.extra) from None
        stored = "file:%s" % written
    else:
        stored = "memory (lost on restart)"

    return {
        "address": account["address"],
        "public_key": account["public_key"],
        "label": label,
        "stored": stored,
        "custody": CUSTODY_SENTENCE,
        "validated": True,
    }


# ---------------------------------------------------------------- network

def balance(address: str, node: nanonode.NanoNode) -> dict:
    """Validate first. A bad address makes NO network call - assert that in tests."""
    require_valid(address)
    try:
        info = node.account_info(address)
        pending = node.receivable(address, MAX_RECEIVE_BLOCKS)
    except nanonode.NodeError as exc:
        raise ToolError(exc.reason, exc.message, 503) from None
    pending_raw = sum(int(block["amount_raw"]) for block in pending)
    return {
        "address": address,
        "balance_xno": _six(info.get("balance_raw", 0)),
        "pending_xno": _six(pending_raw),
        "block_count": int(info.get("block_count", 0)),
        "confirmed": bool(info.get("confirmed", True)),
    }


def receive(address: str, node: nanonode.NanoNode, keys: _keystore.KeyStore,
            max_blocks: int = 8, representative: str = None) -> dict:
    """Pocket receivable blocks. Receiving is not spending: no spend gate here.

    Each block is built, hashed and signed in this process; the node is
    handed an already-signed block and never the key.
    """
    public_key = require_valid(address)
    try:
        max_blocks = int(max_blocks)
    except (TypeError, ValueError):
        raise ToolError("invalid_max_blocks", "max_blocks must be an integer 1..64") from None
    if not 1 <= max_blocks <= MAX_RECEIVE_BLOCKS:
        raise ToolError("invalid_max_blocks",
                        "max_blocks must be between 1 and %d, got %d"
                        % (MAX_RECEIVE_BLOCKS, max_blocks))
    try:
        private_key = keys.get(address)
    except _keystore.KeyStoreError as exc:
        raise ToolError(exc.reason, exc.message) from None

    try:
        pending = node.receivable(address, max_blocks)
        info = node.account_info(address)
        # This pairs the node's balance with the node's frontier the same way
        # `send` does, and deliberately carries no `balance_is_frontier_balance`
        # refusal, because a receive built on a mixed pair fails closed at the
        # node instead of moving XNO. Nano checks a receive's balance increase
        # against the linked block's amount and checks the declared subtype
        # against the direction of the change - so a balance taken one block
        # early either understates the increase or turns the block into a send,
        # and `process` is told `subtype: receive` either way, so the node
        # rejects it. A send has no such check: its amount IS the balance
        # difference, whatever that difference turns out to be, which is why
        # the refusal lives on that path alone. If `process` ever stops sending
        # the subtype (pinned by a test), this comment stops being true.
        balance_raw = int(info.get("balance_raw", 0))
        previous = bytes.fromhex(info["frontier"]) if info.get("frontier") else blocks.ZERO32
        rep_pk = _representative_pk(representative, info.get("representative"), public_key)

        received = []
        for block in pending:
            amount_raw = int(block["amount_raw"])
            balance_raw += amount_raw
            signed = blocks.build_signed(
                private_key, public_key, previous, rep_pk, balance_raw,
                bytes.fromhex(block["hash"]),
                "receive" if previous != blocks.ZERO32 else "open",
            )
            signed["work"] = node.work_generate(signed.pop("_work_root"),
                                               signed["_subtype"])
            block_hash = signed.pop("_hash")
            node.process(dict(signed))
            previous = bytes.fromhex(block_hash)
            received.append({
                "block_hash": block_hash,
                "amount_xno": _six(amount_raw),
                "from": block.get("source"),
            })
        remaining = node.receivable(address, MAX_RECEIVE_BLOCKS)
    except nanonode.NodeError as exc:
        raise ToolError(exc.reason, exc.message, 503) from None

    return {
        "address": address,
        "received": received,
        "balance_xno": _six(balance_raw),
        "remaining_pending": len(remaining),
    }


def send(source: str, destination: str, amount_xno: str, idempotency_key: str,
         node: nanonode.NanoNode, keys: _keystore.KeyStore, sent: dict,
         env=None, representative: str = None) -> dict:
    """Gated. Refused outright unless NANO_WALLET_ALLOW_SEND is exactly "1".

    Never registered at all in the receive-only profile - see profiles.py.
    """
    env = os.environ if env is None else env
    if str(env.get("NANO_WALLET_ALLOW_SEND", "")).strip() != "1":
        raise ToolError(
            "spend_not_enabled",
            "this install cannot send. Set NANO_WALLET_ALLOW_SEND=1 to enable it. "
            "create_address, validate_address, balance and receive all work without it.",
            403,
        )

    source_pk = require_valid(source)
    require_valid(destination)
    destination_pk = bytes.fromhex(validate_address(destination)["public_key"])

    if not idempotency_key or len(str(idempotency_key)) > 64:
        raise ToolError("invalid_idempotency_key",
                        "idempotency_key is required and must be at most 64 characters")
    try:
        amount_raw = wallet.xno_to_raw(amount_xno)
    except ValueError as exc:
        raise ToolError("invalid_amount", str(exc)) from None
    if amount_raw <= 0:
        raise ToolError("invalid_amount", "amount must be greater than zero")

    # The operator's limit, not the agent's amount. It used to reach xno_to_raw
    # unchecked, outside any try: a typo in the variable ("0.5 XNO", "1,5",
    # "1e-3") raised a bare ValueError whose text reads "amount is not a decimal
    # number" - a complaint about an amount the agent never sent, which it can
    # only answer by trying other amounts, forever. Named here instead, as a
    # refusal only the operator can clear.
    try:
        limit_raw = wallet.xno_to_raw(env.get("NANO_WALLET_MAX_SEND_XNO") or DEFAULT_MAX_SEND_XNO)
    except ValueError as exc:
        raise ToolError(
            "misconfigured_send_limit",
            "this install's send limit NANO_WALLET_MAX_SEND_XNO=%r is not an amount "
            "in XNO (%s), so no amount can be checked against it. Nothing was sent, "
            "and retrying with a different amount will not help: only the operator "
            "can fix this."
            % (env.get("NANO_WALLET_MAX_SEND_XNO"), exc),
            403,
        ) from None
    if amount_raw > limit_raw:
        raise ToolError(
            "send_limit_exceeded",
            "this install refuses to send more than %s XNO in one call "
            "(NANO_WALLET_MAX_SEND_XNO); asked for %s"
            % (wallet.raw_to_xno(limit_raw), wallet.raw_to_xno(amount_raw)),
            403,
        )

    previous_call = sent.get(str(idempotency_key))
    if previous_call is not None:
        if (previous_call["to"], previous_call["amount_raw"]) != (destination, amount_raw):
            raise ToolError(
                "idempotency_conflict",
                "idempotency_key %r was already used to send %s XNO to %s; a repeat with "
                "different parameters is refused"
                % (idempotency_key, _six(previous_call["amount_raw"]), previous_call["to"]),
                409,
            )
        if previous_call.get("result") is None:
            # A block was signed and handed to the node under this key, and the
            # node's reply never arrived. The reply is what is missing, not the
            # block: Nano has no fee and no reversal, so a broadcast whose
            # outcome is unknown may well have landed. Replaying it would
            # rebuild from the NEW frontier and pay the destination a second
            # time - the exact thing the key is here to stop. Settle it from
            # the ledger instead; refuse only when the ledger cannot say.
            return _settle_unknown_send(source, str(idempotency_key), previous_call,
                                        node, sent)
        return dict(previous_call["result"], replayed=True)

    guard = _mandate_guard(env, source)  # after the replay: a replay sends nothing
    if guard is not None:
        try:  # refuse before anything is signed; spend() re-checks at broadcast
            guard.check(destination, amount_raw)
        except _mandate.MandateRefused as exc:
            raise _mandate_refusal(exc) from None

    try:
        private_key = keys.get(source)
    except _keystore.KeyStoreError as exc:
        raise ToolError(exc.reason, exc.message) from None

    record = None
    try:
        info = node.account_info(source)
        # Nano reads a send's amount as previous.balance - block.balance, and
        # `previous` below is this account's frontier. So the balance subtracted
        # from has to be the balance AT that frontier. When the node reports a
        # balance taken at the confirmation height while the frontier is further
        # along - the ordinary state for a second or two after this wallet
        # publishes a block of its own - the gap between the two is paid out of
        # the account on top of the payment: a 1 XNO send out of a tip holding
        # 6 XNO against a confirmed 5 builds balance = 4 and moves 2 XNO. On a
        # feeless, sub-second, irreversible rail there is nothing to undo that
        # with, so refuse while the two disagree rather than pick one for the
        # caller. Which of the two a send should be built on is open in #4; this
        # refuses the only state in which the choice can cost money.
        if info.get("frontier") and not info.get("balance_is_frontier_balance", True):
            raise ToolError(
                "account_state_unsettled",
                "the node reports this account's balance at a different block than "
                "its frontier, so a send built from the two together could move "
                "more XNO than asked for. Nothing was signed. This clears on its "
                "own once the account's latest block is confirmed - retry in a "
                "second or two.",
                409,
            )
        balance_raw = int(info.get("balance_raw", 0))
        if balance_raw < amount_raw:
            raise ToolError("insufficient_balance",
                            "balance is %s XNO, asked to send %s XNO"
                            % (_six(balance_raw), _six(amount_raw)),
                            409, available_xno=_six(balance_raw))
        if not info.get("frontier"):
            raise ToolError("insufficient_balance",
                            "this account has never received anything, so it has nothing "
                            "to send", 409, available_xno=_six(0))
        previous = bytes.fromhex(info["frontier"])
        rep_pk = _representative_pk(representative, info.get("representative"), source_pk)
        signed = blocks.build_signed(private_key, source_pk, previous, rep_pk,
                                     balance_raw - amount_raw, destination_pk, "send")
        signed["work"] = node.work_generate(signed.pop("_work_root"),
                                               signed["_subtype"])
        block_hash = signed.pop("_hash")
        # Record the attempt BEFORE the broadcast, the way mandate.py's ledger
        # reserves before it spends and for the same reason it gives there: a
        # send whose outcome is unknown has to stay counted, because
        # under-counting is how the same payment goes out twice. Until the
        # node's reply arrives this entry carries no result, which is what
        # makes the replay above a refusal rather than a second send.
        # `previous` and the signed block itself are kept so that a retry can
        # settle an unknown outcome: publish this same block again (one hash,
        # so at most one payment), or know that its slot was taken.
        #
        # Written inside the broadcast step, never before it: a record with no
        # result is what a retry reads as "published, reply lost" and settles
        # by publishing this block again without asking the mandate a second
        # time. So it must not exist for a block the mandate refused when it
        # re-checked the cap at reservation - that retry would publish a
        # refused block past the operator's cap.
        holder = _key_holding(sent, block_hash, str(idempotency_key))
        if holder is not None:
            raise _duplicate(str(idempotency_key), holder, block_hash)
        record = {"to": destination, "amount_raw": amount_raw,
                  "result": None, "block_hash": block_hash,
                  "previous": previous.hex().upper(),
                  "block": dict(signed)}

        def broadcast():
            sent[str(idempotency_key)] = record
            return node.process(dict(signed))

        if guard is None:
            broadcast()
        else:
            # Reserved in the mandate ledger BEFORE the block is broadcast; a
            # broadcast that raises stays counted, because it may have landed.
            # A refused reservation raises before broadcast() runs.
            guard.spend(destination, amount_raw, broadcast, ref=str(idempotency_key))
    except _mandate.MandateRefused as exc:
        raise _mandate_refusal(exc) from None
    except nanonode.NodeError as exc:
        if exc.reason == "block_rejected" and record is not None:
            # The node read the block and rejected it as invalid: it can never
            # land, so it is not an unknown broadcast and must not hold the key.
            # A record left here would be republished, unchanged and invalid,
            # by every retry of this key, and would refuse every other key on
            # the same terms as `duplicate_send` (identical terms sign to this
            # same hash). A lost reply is NOT this, and still holds the key.
            raise _rejected(sent, str(idempotency_key), record, exc) from None
        raise ToolError(exc.reason, exc.message, 503) from None

    result = {
        "block_hash": block_hash,
        "amount_xno": _six(amount_raw),
        "to": destination,
        "confirmed": False,
        "receipt": "https://nanolooker.com/block/%s" % block_hash,
    }
    sent[str(idempotency_key)] = {"to": destination, "amount_raw": amount_raw,
                                  "result": result, "block_hash": block_hash}
    return dict(result)


def _rejected(sent: dict, idempotency_key: str, record: dict, exc) -> ToolError:
    """The node rejected this key's block as invalid: drop its record, say so."""
    if sent.get(idempotency_key) is record:
        del sent[idempotency_key]
    return ToolError(
        "send_rejected",
        "the node rejected block %s as invalid (%s), so it can never land and nothing "
        "was paid under idempotency_key %r. The key is free: retrying this same call "
        "signs a new block. If the same rejection comes back, the cause is in the "
        "block itself (its work or signature), not in the network."
        % (record.get("block_hash") or "(unknown)", exc.message, idempotency_key),
        422,
    )


def _unknown(idempotency_key: str, record: dict, why: str) -> ToolError:
    return ToolError(
        "send_outcome_unknown",
        "idempotency_key %r already broadcast a send of %s XNO to %s as block %s, "
        "and the node's reply did not arrive, so it is not known whether it "
        "landed (%s). Nothing new was signed. Retry this same call to look it up "
        "again. Do not switch to a NEW idempotency_key while this is unknown: "
        "that would build a second send and may pay twice."
        % (idempotency_key, _six(record["amount_raw"]), record["to"],
           record.get("block_hash") or "(unknown)", why),
        409,
    )


def _settle_unknown_send(source: str, idempotency_key: str, record: dict,
                         node: nanonode.NanoNode, sent: dict) -> dict:
    """Turn a send whose reply was lost into a known outcome, from the ledger.

    Three facts, none of which can be mistaken for another:
      * the block is on the ledger: the payment is made;
      * it is not, and the account's frontier is still the block's `previous`:
        it can still be the next block, so the SAME signed block is published
        again - one hash, so it can only ever be one payment;
      * it is not, and a CONFIRMED block other than it was built on its
        `previous`: the slot it named is taken and it can never land - retry
        under a new key. An occupant that is not confirmed yet is a fork ours
        may still win, so that stays unknown.
    A lookup that fails leaves it unknown. Absence from the ledger alone is
    never read as "never sent"; only a slot taken for good is. A rebroadcast
    the node rejects as an invalid block frees the key (`send_rejected`).
    """
    block_hash = record.get("block_hash")
    if not block_hash:
        raise _unknown(idempotency_key, record, "no block hash was recorded")
    block_hash = block_hash.upper()
    # Identical terms on the same frontier sign to the identical block. If
    # another key has already settled this hash, the one payment it can make
    # is that key's; settling it here as well would report it twice.
    holder = _key_holding(sent, block_hash, idempotency_key, settled_only=True)
    if holder is not None:
        raise _duplicate(idempotency_key, holder, block_hash)
    try:
        found = node.block_info(block_hash)
        if found:
            outcome = "landed"
            confirmed = bool(found.get("confirmed"))
        else:
            if not record.get("previous") or not record.get("block"):
                raise _unknown(idempotency_key, record,
                               "the block is not on this node and this record cannot "
                               "tell whether it still can be")
            previous = record["previous"].upper()
            frontier = (node.account_info(source).get("frontier") or "").upper()
            if frontier == block_hash:
                outcome, confirmed = "landed", False
            elif frontier == previous:
                try:
                    node.process(dict(record["block"]))
                except nanonode.NodeError as exc:
                    if exc.reason == "block_rejected":
                        raise _rejected(sent, idempotency_key, record, exc) from None
                    raise
                outcome, confirmed = "rebroadcast", False
            else:
                # The account moved on. That alone does not say our block lost
                # its slot: it may sit under the blocks that came after it, on
                # a node that does not answer for it. Only the block actually
                # built on `previous` says which.
                occupant = _slot_occupant(node, previous, frontier, block_hash)
                if occupant is None:
                    raise _unknown(idempotency_key, record,
                                   "the block is not on this node, the account has moved "
                                   "on to %s, and the node could not show which block "
                                   "was built on %s" % (frontier or "(none)", previous))
                if occupant != block_hash and not (node.block_info(occupant) or {}).get("confirmed"):
                    # Two blocks on one `previous` are a fork until the network
                    # confirms one of them, and ours can still win it. Saying
                    # "did not land" now tells the agent to pay again under a
                    # new key - two payments if ours wins. Only a CONFIRMED
                    # occupant proves ours never can.
                    raise _unknown(idempotency_key, record,
                                   "block %s was built on %s in its place, but it is not "
                                   "confirmed, so this block may still win that fork"
                                   % (occupant, previous))
                if occupant != block_hash:
                    raise ToolError(
                        "send_did_not_land",
                        "idempotency_key %r broadcast block %s, which is not on the "
                        "ledger: block %s was built on %s in its place and is confirmed, "
                        "so it can never "
                        "land and nothing was paid under this key. To pay, retry with a "
                        "NEW idempotency_key."
                        % (idempotency_key, block_hash, occupant, previous),
                        409,
                    )
                outcome, confirmed = "landed", False
    except nanonode.NodeError as exc:
        raise _unknown(idempotency_key, record, exc.message) from None

    result = {
        "block_hash": block_hash,
        "amount_xno": _six(record["amount_raw"]),
        "to": record["to"],
        "confirmed": confirmed,
        "receipt": "https://nanolooker.com/block/%s" % block_hash,
    }
    sent[idempotency_key] = {"to": record["to"], "amount_raw": record["amount_raw"],
                             "result": result, "block_hash": block_hash}
    return dict(result, replayed=True, reconciled=outcome)


# How far back from the frontier a settle will read the chain to find which
# block took a slot, when the node does not report `successor`. Past this the
# outcome stays unknown rather than guessed.
_WALK_LIMIT = 64


def _slot_occupant(node: nanonode.NanoNode, previous: str, frontier: str, ours: str):
    """The hash of the block built on `previous` on this account, or None.

    Read from the ledger, never inferred: the node's `successor` for
    `previous` when it reports one, otherwise the chain read back from the
    frontier one `previous` at a time. None when neither reaches it.
    """
    successor = (node.block_info(previous) or {}).get("successor")
    if successor:
        return successor.upper()
    cursor = frontier
    for _ in range(_WALK_LIMIT):
        if not cursor:
            return None
        if cursor == ours:
            return ours
        back = ((node.block_info(cursor) or {}).get("previous") or "").upper()
        if not back or not back.strip("0"):
            return None
        if back == previous:
            return cursor
        cursor = back
    return None


def _key_holding(sent: dict, block_hash: str, idempotency_key: str, settled_only: bool = False):
    """Another idempotency key already recorded with this block hash, or None.

    `settled_only` counts only a key whose payment is known (it has a result):
    of two keys left holding one unknown block, the first to settle takes it.
    """
    for key, record in list(sent.items()):
        if key == idempotency_key or str(record.get("block_hash") or "").upper() != block_hash.upper():
            continue
        if settled_only and record.get("result") is None:
            continue
        return key
    return None


def _duplicate(idempotency_key: str, holder: str, block_hash: str) -> ToolError:
    return ToolError(
        "duplicate_send",
        "idempotency_key %r would publish block %s, the identical block idempotency_key "
        "%r already holds (same account, frontier, destination and amount sign to the "
        "same block, and one block is one payment: %r's). Nothing was published by "
        "this call and no payment is counted under %r. Retry %r to learn that "
        "payment's outcome; once it has landed, a new key builds a new block."
        % (idempotency_key, block_hash, holder, holder, idempotency_key, holder),
        409,
    )


# ---------------------------------------------------------------- operator mandate

def _mandate_guard(env, source: str):
    """The operator's signed spend cap for `source`, or None when none is configured.

    NANO_WALLET_MANDATE           path to a signed mandate (see mandate.py)
    NANO_WALLET_MANDATE_LEDGER    its spend ledger (default: <mandate>.ledger.json)
    NANO_WALLET_REQUIRE_MANDATE=1 refuse every send that has no mandate

    A configured mandate that does not verify - bad signature, another agent,
    expired, unreadable - refuses the send. It never falls back to no mandate.
    """
    path = str(env.get("NANO_WALLET_MANDATE", "") or "").strip()
    if not path:
        if str(env.get("NANO_WALLET_REQUIRE_MANDATE", "")).strip() == "1":
            raise ToolError("mandate_required",
                            "NANO_WALLET_REQUIRE_MANDATE=1 and no NANO_WALLET_MANDATE is set: "
                            "this install sends only under an operator-signed mandate", 403)
        return None
    try:
        return _mandate.MandateGuard.from_file(
            path, str(env.get("NANO_WALLET_MANDATE_LEDGER", "") or "").strip() or None, agent=source)
    except _mandate.MandateRefused as exc:
        raise _mandate_refusal(exc) from None


def _mandate_refusal(exc) -> ToolError:
    return ToolError("mandate_refused", "operator mandate refused this send: %s" % exc.message,
                     403, mandate_reason=exc.reason)


# ---------------------------------------------------------------- helpers

def _six(raw: int) -> str:
    """Exact raw -> XNO, rendered at no fewer than 6 decimal places.

    Six is what the spec's worked examples show. Nothing is rounded: a
    balance with more than six significant decimals keeps every one of
    them, because the string is built from the integer, not from a float.
    """
    text = wallet.raw_to_xno(int(raw))
    whole, _, frac = text.partition(".")
    return "%s.%s" % (whole, frac.ljust(6, "0"))


def _representative_pk(explicit: str, current: str, own_public_key: bytes) -> bytes:
    """Who this account votes for. Defaults to itself.

    Self-representation is the choice that involves no third party, which
    is the same reason the rest of this package makes no network call it
    does not need. NANO_WALLET_REPRESENTATIVE overrides it; an account
    that already has a representative keeps it, because changing it is
    not something a receive should ever do silently.
    """
    chosen = explicit or os.environ.get("NANO_WALLET_REPRESENTATIVE") or current
    if not chosen:
        return own_public_key
    verdict = validate_address(chosen)
    if not verdict["valid"]:
        raise ToolError("invalid_representative",
                        "representative %r fails its checksum: %s" % (chosen, verdict["message"]))
    return bytes.fromhex(verdict["public_key"])
