"""Command line entry point: nano-wallet <command>

    check <address>             checksum-verify an address (exit 0 valid, 1 invalid)
    new                         generate a seed and an account, locally
    derive <seed_hex> [index]   derive a further account from a seed
    raw <amount_xno>            exact XNO -> raw
    xno <amount_raw>            exact raw -> XNO

    capabilities [--profile P]  print the capability document for a profile
    selfcheck [--profile P] [--json]
                                verify this install against a profile
                                (exit 0 only if every check passes)
    wellknown [--host H] [--port N]
                                serve GET /.well-known/nano-receive-only

Every command prints one JSON object on stdout, except `selfcheck` without
--json, which prints the operator-readable report.
"""

import json
import sys

import capabilities
import profiles
import selfcheck as _selfcheck
import wallet


def _out(obj, code=0):
    print(json.dumps(obj, indent=2, sort_keys=True))
    return code


def _profile_arg(args, capabilities_default=False) -> str:
    """--profile P / --profile=P, defaulting to receive-only."""
    profile = profiles.RECEIVE_ONLY
    rest = list(args)
    while rest:
        if rest[0] == "--profile" and len(rest) > 1:
            profile, rest = rest[1], rest[2:]
        elif rest[0].startswith("--profile="):
            profile, rest = rest[0].split("=", 1)[1], rest[1:]
        else:
            rest = rest[1:]
    return profile


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    command, args = argv[0], argv[1:]

    try:
        if command == "check":
            if len(args) != 1:
                return _out({"error": "usage: check <address>"}, 2)
            verdict = wallet.validate(args[0])
            return _out(verdict, 0 if verdict["valid"] else 1)

        if command == "new":
            return _out(wallet.create_wallet())

        if command == "derive":
            if not args or len(args) > 2:
                return _out({"error": "usage: derive <seed_hex> [index]"}, 2)
            index = int(args[1]) if len(args) == 2 else 0
            return _out(wallet.derive_account(args[0], index))

        if command == "raw":
            if len(args) != 1:
                return _out({"error": "usage: raw <amount_xno>"}, 2)
            return _out({"xno": args[0], "raw": str(wallet.xno_to_raw(args[0]))})

        if command == "capabilities":
            profile = _profile_arg(args, capabilities_default=True)
            try:
                print(capabilities.as_json(profile))
            except capabilities.UnknownProfile as exc:
                return _out({"error": "unknown_profile", "message": str(exc),
                             "known_profiles": list(profiles.PROFILE_NAMES)}, 2)
            return 0

        if command == "selfcheck":
            return _selfcheck.main(args)

        if command == "wellknown":                          # pragma: no cover - a loop
            import wellknown
            return wellknown.main(args)

        if command == "xno":
            if len(args) != 1:
                return _out({"error": "usage: xno <amount_raw>"}, 2)
            return _out({"raw": args[0], "xno": wallet.raw_to_xno(int(args[0]))})

    except (ValueError, wallet.InvalidAddress) as exc:
        return _out({"error": str(exc)}, 2)

    return _out({"error": "unknown command %r" % command}, 2)


if __name__ == "__main__":
    sys.exit(main())
