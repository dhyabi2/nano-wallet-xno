# From an Ed25519 agent identity to a receive-only Nano address

For nodes that already have an Ed25519 identity (for example a signed agent card) and a human steward who must approve
any payment address. Tool: [`identity_address.py`](../identity_address.py); tests: `tests/test_identity_address.py`.

1. **Do not re-encode the identity public key as a `nano_` address.** Nano is Ed25519 with BLAKE2b-512 where RFC 8032 uses SHA-512.
   One secret gives two different public keys under the two hashes (the test proves it). An address made from the SHA-512 key has no
   private key anyone can sign with, so funds sent there are unrecoverable.
2. **Derive instead, one way:** `nano_seed = BLAKE2b-256("xno-identity-to-nano/v1" || identity_secret)`, account index 0.
3. Run `python3 identity_address.py --secret-file identity.key`. It prints the address and public key only; never the seed or the secret.
4. The steward approves **one address** and one rule: *receive-only*. Publishing an address is not spending authority. Spending needs the
   same derivation on a machine the steward controls, behind its own human-in-the-loop gate.
5. Anyone can check the address is well formed with `nano_keygen.py check <address>`; the node proves it controls it later by signing a challenge
   (see `four_check.py` in xno-call-binding: check 4).

Self-test with a public throwaway vector: `python3 identity_address.py --self-test`.
