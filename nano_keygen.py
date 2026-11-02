#!/usr/bin/env python3
"""nano_keygen.py - make a Nano (XNO) account on your own machine. One file, standard library only.

    python3 nano_keygen.py new                  # address on stdout, seed on STDERR (your terminal)
    python3 nano_keygen.py new --seed-file F    # seed to F (mode 0600, refuses to overwrite), address on stdout
    python3 nano_keygen.py new --key-file F     # the private key to F, the file `nano-pay402 --key-file` and
                                                # NANO_WALLET_KEY_FILE read (no seed is shown or kept)
    python3 nano_keygen.py check nano_...       # checksum-verify any address (exit 0 valid, 1 invalid)
    python3 nano_keygen.py address <seed_hex>   # the address of a seed you already hold (reads $NANO_SEED if no arg)
    python3 nano_keygen.py selftest             # prove this copy against the official test vector (exit 0 = ok)

Why it is shaped like this: the only thing anyone else ever needs from you is the ADDRESS.
"Send me only an address" is the whole protocol. The seed is shown to you alone: it goes to stderr
(so `nano_keygen.py new > address.txt` captures the address and cannot capture the seed), or to a
file only when you name one, created 0600 and never overwritten. Never paste the seed anywhere:
not into a chat, an issue, a PR, a forum post or a message to an agent or a service. Anyone who has
it can spend everything in the account. No network call is made by any command here.

Derivation (Nano docs): private = blake2b-256(seed || index_be32); public = Ed25519 with SHA-512
replaced by BLAKE2b-512; address = "nano_" + base32(public) + base32(blake2b-40(public) reversed).
"""
import hashlib
import os
import sys

# ---- Ed25519 public-key derivation with Nano's BLAKE2b (RFC 8032, hash swapped) ----
_Q = 2 ** 255 - 19
_D = -121665 * pow(121666, _Q - 2, _Q) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _inv(x):
    return pow(x, _Q - 2, _Q)


def _add(p, q):
    t = _D * p[0] * q[0] * p[1] * q[1] % _Q
    return ((p[0] * q[1] + q[0] * p[1]) * _inv(1 + t) % _Q,
            (p[1] * q[1] + p[0] * q[0]) * _inv(1 - t) % _Q)


def _mul(p, e):
    if e == 0:
        return (0, 1)
    h = _mul(p, e // 2)
    h = _add(h, h)
    return _add(h, p) if e & 1 else h


def _x(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q:
        x = x * _I % _Q
    return _Q - x if x % 2 else x


_BY = 4 * _inv(5) % _Q
_B = (_x(_BY), _BY)


def public_key(private_key: bytes) -> bytes:
    h = hashlib.blake2b(private_key, digest_size=64).digest()[:32]
    a = int.from_bytes(h, "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    x, y = _mul(_B, a)
    return ((y & ~(1 << 255)) | ((x & 1) << 255)).to_bytes(32, "little")


def private_key(seed: bytes, index: int = 0) -> bytes:
    return hashlib.blake2b(seed + index.to_bytes(4, "big"), digest_size=32).digest()


# ---- address codec -------------------------------------------------------------------
_ALPHABET = "13456789abcdefghijkmnopqrstuwxyz"


def _b32(value: int, length: int) -> str:
    return "".join(_ALPHABET[(value >> s) & 31] for s in range((length - 1) * 5, -1, -5))


def _checksum(pub: bytes) -> str:
    return _b32(int.from_bytes(hashlib.blake2b(pub, digest_size=5).digest()[::-1], "big"), 8)


def encode(pub: bytes) -> str:
    return "nano_" + _b32(int.from_bytes(pub, "big"), 52) + _checksum(pub)


def check(address: str) -> dict:
    """Never raises. {"valid": True, "public_key": HEX} or {"valid": False, "reason": CODE}."""
    if not isinstance(address, str) or not address.strip():
        return {"valid": False, "reason": "empty"}
    text = address.strip()
    for prefix in ("nano_", "xrb_"):
        if text.startswith(prefix):
            rest = text[len(prefix):]
            break
    else:
        return {"valid": False, "reason": "bad_prefix"}
    if len(rest) != 60:
        return {"valid": False, "reason": "bad_length"}
    body, given = rest[:52], rest[52:]
    if any(c not in _ALPHABET for c in rest):
        return {"valid": False, "reason": "bad_character"}
    if body[0] not in "13":
        return {"valid": False, "reason": "bad_padding"}
    value = 0
    for c in body:
        value = (value << 5) | _ALPHABET.index(c)
    pub = value.to_bytes(33, "big")[1:]
    if _checksum(pub) != given:
        return {"valid": False, "reason": "bad_checksum"}
    return {"valid": True, "public_key": pub.hex().upper()}


def account(seed: bytes, index: int = 0) -> dict:
    pub = public_key(private_key(seed, index))
    address = encode(pub)
    if not check(address)["valid"]:  # cannot happen; a generator must never hand out a bad address
        raise AssertionError("generated an address that fails its own checksum")
    return {"address": address, "public_key": pub.hex().upper(), "index": index}


# Official vector (docs.nano.org, "Creating a seed"/"Seed, private keys, public keys and account"):
# seed 32 zero bytes, index 0.
VECTOR_SEED = "00" * 32
VECTOR_PRIVATE = "9F0E444C69F77A49BD0BE89DB92C38FE713E0963165CCA12FAF5712D7657120F"
VECTOR_PUBLIC = "C008B814A7D269A1FA3C6528B19201A24D797912DB9996FF02A1FF356E45552B"
VECTOR_ADDRESS = "nano_3i1aq1cchnmbn9x5rsbap8b15akfh7wj7pwskuzi7ahz8oq6cobd99d4r3b7"


def selftest() -> list:
    """Failures as strings; empty means this copy reproduces the official vector."""
    bad = []
    seed = bytes.fromhex(VECTOR_SEED)
    if private_key(seed).hex().upper() != VECTOR_PRIVATE:
        bad.append("private key differs from the official vector")
    got = account(seed)
    if got["public_key"] != VECTOR_PUBLIC:
        bad.append("public key differs from the official vector")
    if got["address"] != VECTOR_ADDRESS:
        bad.append("address differs from the official vector")
    if not check(VECTOR_ADDRESS)["valid"]:
        bad.append("the official address fails its own checksum")
    flipped = VECTOR_ADDRESS[:-1] + ("1" if VECTOR_ADDRESS[-1] != "1" else "3")
    if check(flipped)["valid"]:
        bad.append("a corrupted checksum was accepted")
    return bad


def _seed_arg(argv):
    text = argv[0] if argv else os.environ.get("NANO_SEED", "")
    try:
        seed = bytes.fromhex(text.strip())
    except ValueError:
        seed = b""
    return seed if len(seed) == 32 else None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"
    if cmd == "new":
        files = {}
        for flag in ("--seed-file", "--key-file"):
            if flag in argv:
                i = argv.index(flag)
                if i + 1 >= len(argv):
                    print("%s needs a path" % flag, file=sys.stderr)
                    return 2
                files[flag] = argv[i + 1]
        seed = os.urandom(32)
        acct = account(seed)
        contents = {"--seed-file": seed.hex().upper(),
                    "--key-file": private_key(seed).hex().upper()}
        for flag, path in files.items():
            try:
                fd = os.open(os.path.expanduser(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                print("refusing to overwrite %s (an overwritten key is funds that are gone)" % path,
                      file=sys.stderr)
                return 2
            with os.fdopen(fd, "w") as f:
                f.write(contents[flag] + "\n")
            print("%s written to %s (mode 0600). Back it up; never share it."
                  % ("seed" if flag == "--seed-file" else "private key", path), file=sys.stderr)
        if not files:
            print("SEED (for you only - never paste it anywhere, never send it to anyone):\n  %s"
                  % seed.hex().upper(), file=sys.stderr)
        print("Share only the ADDRESS below.", file=sys.stderr)
        print(acct["address"])
        return 0
    if cmd == "check":
        if not argv:
            print("usage: nano_keygen.py check <address>", file=sys.stderr)
            return 2
        verdict = check(argv[0])
        print(("valid " + argv[0]) if verdict["valid"] else "INVALID (%s)" % verdict["reason"])
        return 0 if verdict["valid"] else 1
    if cmd == "address":
        seed = _seed_arg(argv)
        if seed is None:
            print("need a 64-hex seed (argument or $NANO_SEED)", file=sys.stderr)
            return 2
        print(account(seed)["address"])
        return 0
    if cmd == "selftest":
        bad = selftest()
        for line in bad:
            print("FAIL:", line)
        print("selftest: %s" % ("ok - matches the official vector" if not bad else "FAILED"))
        return 1 if bad else 0
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
