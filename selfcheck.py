"""`nano-wallet selfcheck --profile receive-only` - the deliverable.

Seven checks, no network, exit 0 only if every one holds. The last line
is what an outside agent reads to its operator and pastes into an
approval request:

    selfcheck: 7/7 pass - this install can receive XNO and cannot spend it.

It checks the install it is RUNNING ON, not a hypothetical. If the
environment configures the full profile and receive-only is asserted,
this fails and says the install is NOT receive-only - which is the whole
point of shipping a verifier rather than a claim.

`no_sockets()` is how check 5 proves address creation is offline: any
attempt to construct a socket inside it raises. It is a guard, not a
mock - if a future edit adds a network call to create_address, this
check goes red.
"""

import contextlib
import json
import socket
import sys

import keystore as _keystore
import mcp_server
import nanoaddr
import payments
import profiles

#: The burn address. Every character of it is fixed by the protocol, so it is a
#: known-answer vector the swarm cannot influence: its public key is 32 zero
#: bytes and its checksum is whatever blake2b says of them.
KNOWN_GOOD = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"

CHECK_COUNT = 7

PASS_SUMMARY = "this install can receive XNO and cannot spend it."
FAIL_SUMMARY = "this install is NOT receive-only."


class NetworkAttempted(AssertionError):
    pass


@contextlib.contextmanager
def no_sockets():
    """Make every socket construction inside the block raise."""
    real_socket, real_create = socket.socket, socket.create_connection

    def refuse(*args, **kwargs):
        raise NetworkAttempted("a network call was attempted where none is permitted")

    socket.socket = refuse
    socket.create_connection = refuse
    try:
        yield
    finally:
        socket.socket, socket.create_connection = real_socket, real_create


def _altered(address: str) -> str:
    """The same address with one character of its checksum changed.

    Changing the LAST character alters the checksum itself, so the result
    is well-formed in every other respect - the exact failure eddie_
    researcher's address had, and the one a length or charset check misses.
    """
    body = nanoaddr.ALPHABET
    last = address[-1]
    return address[:-1] + (body[(body.index(last) + 1) % len(body)])


def run(profile: str = profiles.RECEIVE_ONLY, env=None) -> dict:
    """Run the checks. Never raises; the verdict is the return value."""
    effective = profiles.ambient(env, profile)
    checks = []

    def record(name, passed, detail=""):
        checks.append({"name": name, "pass": bool(passed), "detail": detail})

    expected = list(profiles.PROFILE_TOOLS.get(profile, ()))

    # The server is constructed under the EFFECTIVE profile, which is what this
    # install would really serve, and measured against the ASSERTED one.
    server = None
    construction_error = ""
    try:
        server = mcp_server.Server(effective, env=env or {})
        registered = server.tool_names()
    except profiles.ProfileError as exc:
        registered = []
        construction_error = exc.message
    except Exception as exc:                                # pragma: no cover - defensive
        registered = []
        construction_error = str(exc)

    # 1 -- the tool set is exactly the profile's
    record("tools registered", registered == expected and not construction_error,
           construction_error or ", ".join(registered) or "none")

    # 2 -- nothing that can move money out is registered
    spend = sorted(profiles.SPEND_TOOLS & set(registered))
    record("tool 'send' is NOT registered", not spend,
           "send is registered" if spend else "absent from tools/list")

    # 3 -- a known-good address validates
    with no_sockets():
        good = payments.validate_address(KNOWN_GOOD)
    record("known-good address validates", good.get("valid") is True,
           KNOWN_GOOD[:12] + "..." + KNOWN_GOOD[-8:])

    # 4 -- one altered character is caught
    with no_sockets():
        bad = payments.validate_address(_altered(KNOWN_GOOD))
    record("altered address rejected",
           bad.get("valid") is False and bad.get("reason") == "invalid_checksum",
           "reason=%s" % bad.get("reason"))

    # 5 -- an address is created with no network call at all
    #
    # The key store is handed in so check 6 can name the exact private key
    # that was generated. Comparing against the real key is the only honest
    # way to assert absence: a "does any 64-hex run appear?" heuristic would
    # fire on the public key, which is 64 hex characters and is meant to be
    # in the output.
    store = _keystore.KeyStore()
    created, creation_detail = None, ""
    try:
        with no_sockets():
            created = payments.create_address(label="selfcheck", store="memory", keys=store)
        creation_detail = "%s  (no network call was made)" % created["address"][:9]
    except NetworkAttempted as exc:
        creation_detail = str(exc)
    except Exception as exc:                                # pragma: no cover - defensive
        creation_detail = "create_address failed: %s" % exc
    record("created a new address offline", created is not None, creation_detail)

    # 6 -- no key material in anything produced above
    blob = json.dumps(checks) + json.dumps(created or {}) + json.dumps(good) + json.dumps(bad)
    leaked = created is not None and _leaks(blob, store.get(created["address"]))
    record("private key absent from all output", not leaked,
           "the generated key appears in the output" if leaked else "none in any field")

    # 7 -- send authority is unavailable, whatever the environment says
    record("send authority: unavailable in this profile",
           not spend and not construction_error,
           "not registered" if not spend else "registered in profile %r" % effective)

    passed = sum(1 for check in checks if check["pass"])
    ok = passed == len(checks) and profile == effective
    if profile != effective and not construction_error:
        # Asserting receive-only on an install configured for something else is
        # itself a failure, even if every individual check were to hold.
        for check in checks:
            if check["name"] == "tools registered":
                check["detail"] += "  (install is configured for profile %r)" % effective
    return {
        "profile": profile,
        "effective_profile": effective,
        "checks": checks,
        "pass": ok,
        "summary": "selfcheck: %d/%d pass - %s"
                   % (passed, len(checks), PASS_SUMMARY if ok else FAIL_SUMMARY),
    }


def _leaks(blob: str, private_key: bytes) -> bool:
    """True if this exact private key appears in the output, in either case."""
    hex_key = private_key.hex()
    return hex_key in blob.lower()


def render(verdict: dict) -> str:
    lines = ["", "nano-wallet selfcheck --profile %s" % verdict["profile"], ""]
    for check in verdict["checks"]:
        lines.append("[%s] %-38s %s"
                     % ("pass" if check["pass"] else "fail", check["name"], check["detail"]))
    lines += ["", verdict["summary"], ""]
    return "\n".join(lines)


def main(argv=None, env=None, out=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    profile, as_json = profiles.RECEIVE_ONLY, False
    while argv:
        if argv[0] == "--profile" and len(argv) > 1:
            profile, argv = argv[1], argv[2:]
        elif argv[0].startswith("--profile="):
            profile, argv = argv[0].split("=", 1)[1], argv[1:]
        elif argv[0] == "--json":
            as_json, argv = True, argv[1:]
        else:
            argv = argv[1:]
    if profile not in profiles.PROFILE_TOOLS:
        out.write("unknown profile %r; known profiles: %s\n"
                  % (profile, ", ".join(profiles.PROFILE_NAMES)))
        return profiles.EXIT_PROFILE_ERROR
    verdict = run(profile, env)
    if not _check_named(verdict, "private key absent from all output"):
        # The one check whose failure stops the run rather than being reported:
        # printing the report would be the leak it is reporting.
        out.write("selfcheck aborted: key material found in its own output\n")
        return 1
    out.write(json.dumps(verdict, indent=2, sort_keys=True) + "\n"
              if as_json else render(verdict))
    return 0 if verdict["pass"] else 1


def _check_named(verdict: dict, name: str) -> bool:
    for check in verdict["checks"]:
        if check["name"] == name:
            return check["pass"]
    return False                                            # pragma: no cover - defensive


if __name__ == "__main__":
    sys.exit(main())
