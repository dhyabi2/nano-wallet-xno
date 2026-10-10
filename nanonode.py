"""The one place in this package that touches the network.

Everything else - address creation, validation, block construction and
signing - runs offline in the caller's process. That separation is the
point: an operator reviewing this install can read this single file to
see every byte that leaves the machine, and `receive-only` mode adds
nothing to it.

`NanoNode` is an interface, not a class to inherit from for its
behaviour. `HttpNanoNode` speaks the standard Nano node RPC over HTTP;
the test-suite substitutes an in-memory fake. No test in this repository
opens a socket.
"""

import json
import urllib.error
import urllib.request

import prework as _prework
import work as _work

DEFAULT_TIMEOUT = 10.0

# Sent on every call. Cloudflare-fronted public nodes answer urllib's default
# User-Agent with 403 "error code: 1010", which reads exactly like a refused key.
USER_AGENT = "nano-wallet-xno/1.1"


class NodeError(Exception):
    """A node was unreachable, or answered something unusable.

    `reason` is the stable machine-readable code from the spec's error
    table. The message NEVER carries the node URL's credentials - only
    its host - because node URLs are routinely of the form
    https://user:key@node.example.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# Answers to `process` that say the block itself is invalid - fixed by its own
# bytes and the block it names as `previous` - so it can never land, here or on
# any other node. Matched case-insensitively as substrings of the node's error.
# Deliberately NOT here, because each leaves the block able to land: "Old block"
# (it is already on the ledger), "Fork" (it is in an election it may still win),
# "Gap ..." (the node may apply it once the block before it arrives), and the
# RPC's subtype checks ("Invalid block balance for given subtype", ...), which
# compare against the account's CURRENT balance, so a block that landed earlier
# fails them once the account has moved on. Anything unrecognised is not a
# verdict either.
# `process` raises these as NodeError("block_rejected"), the one NodeError that is
# a verdict on the block rather than on the connection.
_REJECTIONS = (
    "bad signature",
    "work is less than threshold",
    "insufficient work",
    "negative spend",
    "balance and amount delta do not match",
    "balance mismatch",
    "cannot follow the previous block",
    "block is invalid",
)
_NOT_REJECTIONS = ("old block", "fork", "gap", "subtype")


def is_rejection(error) -> bool:
    """True when a node's `process` error says the block can never be valid."""
    text = str(error or "").lower()
    if any(marker in text for marker in _NOT_REJECTIONS):
        return False
    return any(marker in text for marker in _REJECTIONS)


class NanoNode:
    """The RPC surface this wallet needs. Six calls, no more."""

    def account_info(self, address: str) -> dict:
        """`{"frontier","balance_raw","representative","block_count","confirmed"}`,
        or `{}` for an account that has never been opened.

        An implementation that can tell whether `balance_raw` is the balance at
        `frontier` - rather than at some earlier block on the same chain - also
        returns `balance_is_frontier_balance`. A caller doing arithmetic with
        the two together treats a missing key as True.
        """
        raise NotImplementedError

    def receivable(self, address: str, count: int) -> list:
        """`[{"hash","amount_raw","source"}]`, newest first, at most `count`."""
        raise NotImplementedError

    def work_generate(self, root_hex: str, subtype: str = None) -> str:
        """Work for `root_hex`. `subtype` says which threshold the block needs;
        an implementation may use it to decide whether it can find work itself."""
        raise NotImplementedError

    def process(self, block: dict) -> str:
        """Publish a signed block; return its hash."""
        raise NotImplementedError

    def block_info(self, block_hash: str) -> dict:
        """`{"account","confirmed"}` for a block on the ledger, or `{}` when this
        node does not have it, plus `previous` (the block it builds on) and
        `successor` (the block built on it) when the node reports them. Used
        only to settle a send whose reply was lost.

        `confirmed` must be a bool: the wallet reads only `True` as confirmed,
        so pass the RPC's "true"/"false" strings through as bools, not as text.
        A subclass that does not implement this keeps working; a send whose
        reply was lost and whose account has since moved on then stays
        `send_outcome_unknown` instead of being settled."""
        raise NotImplementedError


def host_of(url: str) -> str:
    """The host of a node URL, with any credentials stripped.

    Used for every message that names the node, so a misconfigured
    `https://user:secret@node/` can never reach a log line or an API
    response.
    """
    try:
        rest = url.split("://", 1)[1] if "://" in url else url
        authority = rest.split("/", 1)[0]
        return authority.rsplit("@", 1)[-1] or "the configured node"
    except Exception:                                   # pragma: no cover - defensive
        return "the configured node"


#: `prework=None` must mean "no cache, whatever the environment says", so the
#: default cannot be None.
_UNSET = object()


class HttpNanoNode(NanoNode):
    """A Nano node's JSON-RPC endpoint, over urllib. No dependencies."""

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT, auth_header: str = None,
                 local_work: bool = True, prework=_UNSET):
        if not url:
            raise NodeError("node_not_configured",
                            "no node URL: set NANO_NODE_URL to a Nano RPC endpoint")
        self.url = url
        self.timeout = timeout
        self.auth_header = auth_header
        # Public nodes refuse `work_generate`, and a new wallet has no other
        # node. Set False to require the node to supply work.
        self.local_work = local_work
        # Work this wallet found before the payment (`prework.py`). None, and
        # every line below that touches it is dead, unless the environment
        # names a cache directory - so an install that has not asked for this
        # behaves exactly as it did before the cache existed. Pass None to
        # refuse it explicitly even when the environment configures one.
        self.prework = _prework.WorkCache.from_env() if prework is _UNSET else prework

    def _rpc(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        if self.auth_header:
            request.add_header("Authorization", self.auth_header)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A 4xx came from the node itself: it was reached and refused
            # (rpc.nano.to answers `work_generate` with 402). That is a node
            # answer, so `work_generate` may still fall back to local work.
            # A 5xx is a node, or the proxy in front of it, that is down.
            if 400 <= exc.code < 500:
                raise NodeError(
                    "node_error",
                    "the Nano node at %s refused with HTTP %d %s"
                    % (host_of(self.url), exc.code, exc.reason),
                ) from None
            raise NodeError(
                "node_unreachable",
                "could not reach the Nano node at %s: HTTP %d %s"
                % (host_of(self.url), exc.code, exc.reason),
            ) from None
        except urllib.error.URLError as exc:
            raise NodeError(
                "node_unreachable",
                "could not reach the Nano node at %s: %s" % (host_of(self.url), exc.reason),
            ) from None
        except (ValueError, OSError) as exc:
            raise NodeError(
                "node_unreachable",
                "the Nano node at %s answered something unusable: %s"
                % (host_of(self.url), exc),
            ) from None
        if isinstance(answer, dict) and "error" in answer:
            # A real node answers `account_info` for an account that has never
            # received with this error, not with an empty account. It is the
            # state every new wallet starts in, so it is an empty answer here
            # (`account_info` turns a missing frontier into `{}`), and only for
            # that one call - anywhere else it is still a node error.
            if payload.get("action") == "account_info" and answer["error"] == "Account not found":
                return {}
            # Likewise `block_info` for a block the node does not have: an
            # answer ("not here"), not a failure to reach the node.
            if payload.get("action") == "block_info" and answer["error"] == "Block not found":
                return {}
            # Only for a send: that is the one block whose record a rejection
            # frees. A receive or an open the node refuses stays a node_error,
            # as it always was.
            if (payload.get("action") == "process" and payload.get("subtype") == "send"
                    and is_rejection(answer["error"])):
                raise NodeError("block_rejected", "the Nano node at %s rejected the block: %s"
                                % (host_of(self.url), answer["error"]))
            raise NodeError("node_error", "the Nano node at %s returned: %s"
                            % (host_of(self.url), answer["error"]))
        return answer

    def account_info(self, address: str) -> dict:
        answer = self._rpc({
            "action": "account_info", "account": address,
            "representative": "true", "include_confirmed": "true",
        })
        if not answer.get("frontier"):
            return {}
        # `frontier` and `balance` are the account's TIP. `confirmed_frontier`
        # and `confirmed_balance` are the account at its confirmation height.
        # They differ whenever a block of ours has not been confirmed yet -
        # for the second or two after we publish a receive, say.
        #
        # They must be read as a PAIR. This returned the tip's frontier beside
        # the CONFIRMED balance, and `blocks.build_signed` uses the frontier as
        # `previous` and the balance to compute the new one. Nano reads the
        # amount of a send as `previous.balance - block.balance`, so the
        # mismatch is paid out of the account: receive 1 XNO, then send 1 XNO
        # before that receive confirms, and previous.balance is 6 XNO while the
        # block says 5 - 1 = 4, so 2 XNO leaves. The agent asked to send 1.
        #
        # The tip is the pair to use. `previous` has to BE the account's tip or
        # the block forks our own chain, and the unconfirmed part of the tip is
        # our own just-published blocks: `receivable` below asks for
        # `include_only_confirmed`, so we only ever receive sends the network
        # has already confirmed. `confirmed` now says whether the tip itself is
        # confirmed, which is the question a caller was asking all along - it
        # used to be `confirmed_height is not None`, true for every opened
        # account, including this one - and it fails closed: see
        # `_tip_is_confirmed`.
        frontier = answer["frontier"]
        return {
            "frontier": frontier,
            "balance_raw": int(answer["balance"]),
            "representative": answer.get("representative"),
            "block_count": int(answer.get("block_count", 0)),
            "confirmed": _tip_is_confirmed(answer, frontier),
            # #10 added this flag and a `send` refusal for the state this pull
            # request removes: the frontier and the balance used to come from two
            # different moments, so `send` refused while they disagreed rather
            # than paying the gap out of the account. With the tip read as a pair
            # above, `balance_raw` IS the balance at `frontier`, always - so the
            # flag is unconditionally true and #10's refusal goes quiet. It is
            # reported, and the refusal is kept, deliberately: a later change
            # that reintroduces a mixed pair meets the refusal again instead of
            # silently overpaying. This is the resolution #10's own commit
            # message asked for.
            "balance_is_frontier_balance": True,
        }

    def receivable(self, address: str, count: int) -> list:
        answer = self._rpc({
            "action": "receivable", "account": address, "count": str(count),
            "source": "true", "include_only_confirmed": "true",
        })
        blocks = answer.get("blocks") or {}
        if not isinstance(blocks, dict):        # the node answers "" for none
            return []
        out = []
        for block_hash, info in blocks.items():
            if isinstance(info, dict):
                out.append({"hash": block_hash,
                            "amount_raw": int(info["amount"]),
                            "source": info.get("source")})
            else:
                out.append({"hash": block_hash, "amount_raw": int(info), "source": None})
        return out[:count]

    def work_generate(self, root_hex: str, subtype: str = None) -> str:
        """Ask the node, and fall back to local work for a receive or an open.

        A public node answers `work_generate` with an error or with nothing,
        and that is the only node a brand-new wallet has - it owns no XNO yet,
        so its first operation is a receive. Nano asks 64x less work of a
        receive or open block than of a send, and that much is findable here
        in seconds, so the node refusing is no longer the end of the road.

        It stays a fallback, not a replacement: a node that generates work is
        asked first and is faster. Nothing that spends falls back (see
        `work.LOCAL_SUBTYPES`) - at the send threshold this would take minutes,
        and a send whose node owes it work should say so, not stall.

        Work found BEFORE the payment is different, and is read first of all:
        it is already paid for, it is verified against this root and this
        subtype's threshold before it is used, and reading a file beats a
        network round trip. That is the one route by which a send gets work
        without a node - see `prework.py`.
        """
        cached = self._precomputed(root_hex, subtype)
        if cached:
            return cached
        try:
            answer = self._rpc({"action": "work_generate", "hash": root_hex})
            work = answer.get("work")
        except NodeError as exc:
            # The node answered and refused (work generation disabled, no work
            # peer). Unreachable is different: the block could not be published
            # either, so the caller needs that error, not a minute of hashing.
            if exc.reason != "node_error":
                raise
            work = None
        if work:
            return work
        return self._local_work(root_hex, subtype)

    def _precomputed(self, root_hex: str, subtype: str = None):
        """Work found ahead of this payment, or None.

        A cache that cannot be read is a miss, never an error: the node is
        still there, and a wallet must not fail to pay because a directory
        was removed between two calls.
        """
        # `local_work=False` means this wallet does no proof-of-work of its
        # own and the node must supply it. Work out of the cache IS work this
        # wallet did, just earlier, so that switch turns the cache off too -
        # the conservative reading, and the one that cannot surprise a caller
        # who disabled local work on purpose.
        if self.prework is None or not self.local_work:
            return None
        try:
            return self.prework.get(root_hex, _prework.threshold_for(subtype))
        except Exception:                                   # pragma: no cover - defensive
            return None

    def _local_work(self, root_hex: str, subtype: str = None) -> str:
        refused = NodeError(
            "work_unavailable",
            "the node at %s did not return proof-of-work; enable work generation "
            "on it or set a work peer" % host_of(self.url))
        if not self.local_work or subtype not in _work.LOCAL_SUBTYPES:
            raise refused
        try:
            root = bytes.fromhex(root_hex)
        except ValueError:
            raise NodeError("bad_work_root",
                            "work root must be 64 hex characters") from None
        try:
            return _work.solve(root, _work.RECEIVE_THRESHOLD)
        except (_work.WorkUnavailable, ValueError) as exc:
            raise NodeError("work_unavailable",
                            "the node at %s did not return proof-of-work and none was "
                            "found here: %s" % (host_of(self.url), exc)) from None

    def process(self, block: dict) -> str:
        answer = self._rpc({
            "action": "process", "json_block": "true",
            "subtype": block.pop("_subtype"), "block": block,
        })
        block_hash = answer.get("hash")
        if not block_hash:
            raise NodeError("publish_failed",
                            "the node at %s accepted no block" % host_of(self.url))
        # This block is the account's new frontier, so it is the root of the
        # account's NEXT block - and the next one may be a send, whose work
        # no public node will do. Recording it here is what gives
        # `nano-wallet work precompute` something to work on while nothing is
        # waiting on it. A hint only: it cannot fail the publish.
        if self.prework is not None:
            try:
                self.prework.want(block_hash)
            except Exception:                               # pragma: no cover - defensive
                # The block is on the network. Whatever went wrong filing a
                # hint, the caller must be told the payment went out, not
                # handed an exception that reads like a failed publish.
                pass
        return block_hash

    def block_info(self, block_hash: str) -> dict:
        answer = self._rpc({"action": "block_info", "json_block": "true", "hash": block_hash})
        if not answer.get("block_account"):
            return {}
        found = {"account": answer["block_account"],
                 "confirmed": str(answer.get("confirmed", "false")) == "true"}
        contents = answer.get("contents")
        if isinstance(contents, dict) and _is_hash(contents.get("previous")):
            found["previous"] = contents["previous"].upper()
        # "0" * 64 is the node's way of saying nothing is built on this block
        # yet; it is not a block, so it is left out rather than passed on.
        successor = answer.get("successor")
        if _is_hash(successor) and successor.strip("0"):
            found["successor"] = successor.upper()
        return found


def _tip_is_confirmed(answer: dict, frontier: str) -> bool:
    """Whether the network has confirmed the account's latest block.

    Fails closed: True only when the node's answer shows it. The confirmed
    frontier is compared first (`confirmed_frontier`, then the older
    `confirmation_height_frontier`), case-insensitively, since hex from a node
    is not promised to be upper case. Without either, the heights: Nano numbers
    an account's blocks from 1 (the open block), so the tip's height IS
    `block_count`, and the tip is confirmed exactly when the confirmation height
    has reached it. `confirmed_height` comes with include_confirmed and
    `confirmation_height` without it; they are the same number. A node that
    says neither has not told us the tip is confirmed, so it is not reported as
    though it were.
    """
    confirmed_frontier = answer.get("confirmed_frontier") or \
        answer.get("confirmation_height_frontier")
    if confirmed_frontier:
        return str(confirmed_frontier).upper() == str(frontier).upper()
    height = answer.get("confirmed_height")
    if height is None:
        height = answer.get("confirmation_height")
    count = answer.get("block_count")
    if height is None or count is None:
        return False
    try:
        return int(count) > 0 and int(height) == int(count)
    except (TypeError, ValueError):
        return False


def _is_hash(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True
