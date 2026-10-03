"""Proof-of-work for a block, found locally when the node will not do it.

Public nodes refuse `work_generate`. That is the whole of why a brand-new
wallet could not receive: it has no XNO yet, so its first operation is a
receive, and the only node it has is a public one. Nano asks for much less
work from a receive or open block than from a send - the thresholds below
differ by a factor of 64 - and the receive threshold is reachable here, in
Python, in seconds. The send threshold is not, and is deliberately not
attempted: see `solve`.

Validity, per the protocol: blake2b-64 over the 8 work bytes in REVERSE
order followed by the root, read as a little-endian unsigned integer, must
be at least the threshold. The root is the account's public key for an open
block and the previous block's hash otherwise (`blocks.work_root`).

Checked against a real open block published on the live network -
`BCE621224274F7DCB1B9BFB212CF9E5CADC50B47E8BEA5C2E4ED0F063566D85B`, the one
`tests/test_work.py` pins. Its work clears RECEIVE_THRESHOLD and fails
SEND_THRESHOLD, which is what a receive block's work should do, and no other
byte order validates it at all.
"""

import hashlib
import os
import time

# Nano's epoch-2 thresholds. A send or a change needs 64x the work of a
# receive or an open. Never lower either of these.
SEND_THRESHOLD = 0xFFFFFFF800000000
RECEIVE_THRESHOLD = 0xFFFFFE0000000000

# Subtypes whose work this module will look for locally. Everything that
# spends is absent on purpose - at the measured rate a send takes minutes,
# and a wallet that quietly burns six minutes of CPU before spending is
# worse than one that says the node owes it work.
LOCAL_SUBTYPES = frozenset({"receive", "open"})

# A receive needs 2**23 hashes on average. The default budget is a wall
# clock, not a count, so the bound holds on a slow machine too; at the
# ~1.4M hashes a second this manages it is about ten times the average,
# so giving up is rare and never silent - the caller gets `work_unavailable`.
DEFAULT_BUDGET_SECONDS = 60.0


class WorkUnavailable(Exception):
    """No valid work was found inside the budget."""


def difficulty(root: bytes, work_hex: str) -> int:
    """The work's difficulty, to compare against a threshold."""
    work = bytes.fromhex(work_hex)
    if len(work) != 8:
        raise ValueError("work must be 8 bytes (16 hex characters), got %d" % len(work))
    if len(root) != 32:
        raise ValueError("root must be 32 bytes, got %d" % len(root))
    digest = hashlib.blake2b(work[::-1] + root, digest_size=8).digest()
    return int.from_bytes(digest, "little")


def validates(root: bytes, work_hex: str, threshold: int = RECEIVE_THRESHOLD) -> bool:
    """Whether `work_hex` is valid work for `root` at `threshold`."""
    try:
        return difficulty(root, work_hex) >= threshold
    except ValueError:
        return False


def solve(root: bytes, threshold: int = RECEIVE_THRESHOLD,
          budget_seconds: float = DEFAULT_BUDGET_SECONDS, _now=time.monotonic) -> str:
    """Work for `root` at `threshold`, as the 16 hex characters a block carries.

    Refuses the send threshold outright rather than spending minutes on it:
    a caller that reaches here for a send has a node that owes it work, and
    should be told so while the block is still unsigned.
    """
    if len(root) != 32:
        raise ValueError("root must be 32 bytes, got %d" % len(root))
    if threshold > RECEIVE_THRESHOLD:
        raise WorkUnavailable(
            "local work is only attempted at the receive threshold (%#x); %#x "
            "needs a node that generates work" % (RECEIVE_THRESHOLD, threshold))

    blake2b = hashlib.blake2b                      # bound once; this is the hot loop
    nonce = int.from_bytes(os.urandom(8), "little")
    deadline = _now() + budget_seconds
    attempts = 0
    while True:
        # Checked before the first hash as well as every 8192 after it, so an
        # exhausted budget gives up without doing work - which is what makes
        # this branch testable at all, rather than a 1-in-1000 race.
        if attempts % 8192 == 0 and _now() >= deadline:
            raise WorkUnavailable(
                "no proof-of-work found for this block in %.0f seconds (%d attempts); "
                "use a node that generates work, or set a work peer"
                % (budget_seconds, attempts))
        candidate = nonce.to_bytes(8, "little")
        if int.from_bytes(blake2b(candidate + root, digest_size=8).digest(),
                          "little") >= threshold:
            # The bytes hashed are the work in reverse, so the block carries
            # the reverse of what was hashed.
            return candidate[::-1].hex()
        nonce = (nonce + 1) & 0xFFFFFFFFFFFFFFFF
        attempts += 1
