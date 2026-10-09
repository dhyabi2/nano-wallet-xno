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
        only to settle a send whose reply was lost."""
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


class HttpNanoNode(NanoNode):
    """A Nano node's JSON-RPC endpoint, over urllib. No dependencies."""

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT, auth_header: str = None,
                 local_work: bool = True):
        if not url:
            raise NodeError("node_not_configured",
                            "no node URL: set NANO_NODE_URL to a Nano RPC endpoint")
        self.url = url
        self.timeout = timeout
        self.auth_header = auth_header
        # Public nodes refuse `work_generate`, and a new wallet has no other
        # node. Set False to require the node to supply work.
        self.local_work = local_work

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
        confirmed_balance = answer.get("confirmed_balance")
        return {
            "frontier": answer["frontier"],
            "balance_raw": int(answer["balance"] if confirmed_balance is None
                               else confirmed_balance),
            "representative": answer.get("confirmed_representative", answer.get("representative")),
            "block_count": int(answer.get("block_count", 0)),
            "confirmed": answer.get("confirmed_height") is not None,
            # Does `balance_raw` belong to `frontier`? `frontier`/`balance`
            # describe the account's tip; `confirmed_frontier`/`confirmed_balance`
            # describe it at its confirmation height. They are the same point on
            # the chain only while the tip is confirmed, and this answer takes the
            # frontier from one and the balance from the other - so say which it
            # is, because Nano reads a send's amount as the difference between
            # two balances and `payments.send` pairs them. Reported here rather
            # than repaired: which of the two a wallet should build on is a
            # judgement about a chain it is extending, not about reading JSON
            # (dhyabi2/nano-wallet-xno#4). Unprovable counts as False: a node
            # that sends a confirmed balance and no confirmed frontier has not
            # told us the two match.
            "balance_is_frontier_balance": (
                confirmed_balance is None
                or answer.get("confirmed_frontier") == answer["frontier"]
            ),
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
        """
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


def _is_hash(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True
