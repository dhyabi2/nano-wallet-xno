#!/usr/bin/env python3
"""Receive-only Nano address from an agent's existing Ed25519 identity secret. Stdlib only, no network.

Why not just re-encode the identity PUBLIC key as a nano_ address? Nano is Ed25519 with BLAKE2b-512 in place of
SHA-512. The same 32-byte secret therefore yields a DIFFERENT public key under the two hashes, so an address built
from the identity's SHA-512 public key has no private key anyone can use: money sent there is lost.

Derivation here (domain-separated, one-way, the identity secret is never printed or stored):
    nano_seed = BLAKE2b-256( b"xno-identity-to-nano/v1" || identity_secret_32 )
    account   = standard Nano account at index 0 of that seed
Only the ADDRESS and PUBLIC KEY are printed. The steward approves the address; the node publishes it as
receive-only. Spending later needs the same derivation done by whoever holds the identity secret.

    python3 identity_address.py --secret-file identity.key      # file holds 32 raw bytes or 64 hex chars
    python3 identity_address.py --self-test
"""
import argparse, hashlib, sys
import ed25519_blake2b as ed
import nano_keygen as nk

DOMAIN = b"xno-identity-to-nano/v1"


def nano_seed(identity_secret: bytes) -> bytes:
    if len(identity_secret) != 32:
        raise ValueError("identity secret must be 32 bytes")
    return hashlib.blake2b(DOMAIN + identity_secret, digest_size=32).digest()


def address_for(identity_secret: bytes) -> dict:
    a = nk.account(nano_seed(identity_secret), 0)
    return {"address": a["address"], "public_key": a["public_key"], "index": 0, "derivation": DOMAIN.decode()}


def naive_address(identity_secret: bytes) -> str:
    """What NOT to do: encode the identity's own SHA-512 Ed25519 public key. No usable key exists for it."""
    pub = ed.public_key_from_private(identity_secret, hashfn=ed._sha512)
    return nk.encode(pub)


def _load(path):
    raw = open(path, "rb").read().strip()
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw.decode())
        except ValueError:
            pass
    return raw


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--secret-file")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        s = bytes(range(32))  # public throwaway test vector, never a real identity
        print("derived :", address_for(s)["address"])
        print("naive   :", naive_address(s), "(no key exists for this address; do not use)")
        return 0
    if not a.secret_file:
        sys.exit("need --secret-file or --self-test")
    import json
    print(json.dumps(address_for(_load(a.secret_file)), indent=1))


if __name__ == "__main__":
    main()
