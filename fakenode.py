"""An in-memory Nano node, so no test in this repository opens a socket.

It is a fake, not a mock: it keeps a real chain per account, applies
blocks it is given, and refuses a block whose `previous` does not match
the account's frontier. A test that builds an invalid chain fails here
the way a real node would fail it, which is the only reason a fake is
worth more than a stub.

It is shipped rather than kept in tests/ so that anyone evaluating this
package can drive `receive` end to end without a node.
"""

from hashlib import blake2b

import nanoaddr
import nanonode


class FakeNode(nanonode.NanoNode):
    def __init__(self, fail_with: Exception = None):
        self.accounts = {}       # address -> {"frontier","balance_raw","representative","block_count"}
        self.pending = {}        # address -> [{"hash","amount_raw","source"}]
        self.published = []      # every block handed to process(), in order
        self.calls = []          # every RPC name, in order - assert "no network call"
        self.fail_with = fail_with

    # -- test fixtures ----------------------------------------------------

    def fund(self, address: str, amount_raw: int, source: str = None, block_hash: str = None):
        """Put one receivable block in front of `address`."""
        if block_hash is None:
            seed = "%s:%d:%d" % (address, amount_raw, len(self.pending.get(address, [])))
            block_hash = blake2b(seed.encode(), digest_size=32).hexdigest().upper()
        self.pending.setdefault(address, []).append(
            {"hash": block_hash, "amount_raw": int(amount_raw),
             "source": source or "nano_1111111111111111111111111111111111111111111111111111hifc8npp"}
        )
        return block_hash

    # -- the interface ----------------------------------------------------

    def _guard(self, name):
        self.calls.append(name)
        if self.fail_with is not None:
            raise self.fail_with

    def account_info(self, address: str) -> dict:
        self._guard("account_info")
        return dict(self.accounts.get(address, {}))

    def receivable(self, address: str, count: int) -> list:
        self._guard("receivable")
        return [dict(block) for block in self.pending.get(address, [])[:count]]

    def work_generate(self, root_hex: str, subtype: str = None) -> str:
        self._guard("work_generate")
        if len(root_hex) != 64:
            raise nanonode.NodeError("bad_work_root",
                                     "work root must be 64 hex characters, got %d" % len(root_hex))
        return blake2b(root_hex.encode(), digest_size=8).hexdigest()

    def process(self, block: dict) -> str:
        self._guard("process")
        address = block["account"]
        account = self.accounts.get(address, {})
        expected = account.get("frontier", "0" * 64)
        if block["previous"] != expected:
            raise nanonode.NodeError(
                "fork",
                "previous %s does not match the account frontier %s"
                % (block["previous"][:12], expected[:12]),
            )
        if not block.get("work"):
            raise nanonode.NodeError("work_missing", "block carries no work")
        if not block.get("signature"):
            raise nanonode.NodeError("signature_missing", "block carries no signature")

        block_hash = _hash_of(block)
        if block["link"] != "0" * 64:
            # A receive consumes the pending entry whose hash it links to.
            remaining = [entry for entry in self.pending.get(address, [])
                         if entry["hash"] != block["link"]]
            if len(remaining) != len(self.pending.get(address, [])):
                self.pending[address] = remaining
        self.accounts[address] = {
            "frontier": block_hash,
            "balance_raw": int(block["balance"]),
            "representative": block["representative"],
            "block_count": account.get("block_count", 0) + 1,
            "confirmed": True,
        }
        self.published.append(dict(block))
        return block_hash

    def block_info(self, block_hash: str) -> dict:
        self._guard("block_info")
        for block in self.published:
            if _hash_of(block) == block_hash:
                return {"account": block["account"], "confirmed": True}
        return {}


def _hash_of(block: dict) -> str:
    """Recompute the block hash from the block, the way a node would."""
    digest = blake2b(digest_size=32)
    digest.update((6).to_bytes(32, "big"))
    digest.update(nanoaddr.decode(block["account"]))
    digest.update(bytes.fromhex(block["previous"]))
    digest.update(nanoaddr.decode(block["representative"]))
    digest.update(int(block["balance"]).to_bytes(16, "big"))
    digest.update(bytes.fromhex(block["link"]))
    return digest.hexdigest().upper()
