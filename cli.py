"""Command line entry point: nano-wallet <command>

    check <address>             checksum-verify an address (exit 0 valid, 1 invalid)
    new                         generate a seed and an account, locally
    derive <seed_hex> [index]   derive a further account from a seed
    raw <amount_xno>            exact XNO -> raw
    xno <amount_raw>            exact raw -> XNO

    work rate [--seconds S]     measure this machine's hash rate
    work estimate [--workers N] what a send's proof-of-work costs here
    work pending [--cache DIR]  roots this wallet will need work for
    work precompute [<root_hex>] [--threshold send|receive] [--budget S]
                    [--workers N] [--cache DIR]
                                find a block's work NOW, on every core, and
                                store it; the send that needs it later asks
                                no node for work (see prework.py)

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
import prework
import profiles
import selfcheck as _selfcheck
import wallet
import work as _work


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


def _flag(args, name, convert=str, default=None):
    """--name V / --name=V, converted, or `default`."""
    rest = list(args)
    value = default
    while rest:
        if rest[0] == "--" + name and len(rest) > 1:
            value, rest = rest[1], rest[2:]
        elif rest[0].startswith("--" + name + "="):
            value, rest = rest[0].split("=", 1)[1], rest[1:]
        else:
            rest = rest[1:]
    return default if value is default else convert(value)


def _positional(args) -> list:
    """Everything that is not a flag or a flag's value."""
    out, rest = [], list(args)
    while rest:
        token = rest.pop(0)
        if token.startswith("--"):
            if "=" not in token and rest:
                rest.pop(0)
            continue
        out.append(token)
    return out


def _work_cache(args):
    """The cache --cache or the environment names, or an error object."""
    directory = _flag(args, "cache")
    cache = prework.WorkCache(directory) if directory else prework.WorkCache.from_env()
    if cache is None:
        return None, {"error": "no_work_cache",
                      "message": "set %s to a directory, or pass --cache DIR"
                                 % prework.CACHE_ENV}
    return cache, None


def _work_command(args) -> int:
    """`work ...`: proof-of-work this machine finds for itself.

    Every subcommand here is pure local CPU and touches no node and no key,
    which is why it is in the CLI at all: an operator can measure what a send
    will cost, and fill the cache, on a wallet that holds nothing.
    """
    if not args or args[0] in ("-h", "--help"):
        return _out({"error": "usage: work rate|estimate|pending|precompute [...]"}, 2)
    sub, rest = args[0], args[1:]

    if sub == "rate":
        seconds = _flag(rest, "seconds", float, 0.5)
        rate = _work.measure_rate(seconds)
        return _out({"hashes_per_second": round(rate),
                     "measured_over_seconds": seconds,
                     "cores": _work.worker_count()})

    if sub == "estimate":
        workers = _flag(rest, "workers", int)
        rate = _work.measure_rate(_flag(rest, "seconds", float, 0.5))
        count = _work.worker_count(workers)
        out = {"hashes_per_second": round(rate), "workers": count}
        for name, threshold in (("receive", _work.RECEIVE_THRESHOLD),
                                ("send", _work.SEND_THRESHOLD)):
            out[name] = {
                "threshold": "%#x" % threshold,
                "expected_hashes": round(_work.expected_attempts(threshold)),
                "average_seconds": round(
                    _work.estimate_seconds(threshold, rate, count), 1),
            }
        out["note"] = ("an average, not a bound: the search is memoryless, so "
                       "about one search in twenty takes three times this")
        return _out(out)

    if sub == "pending":
        cache, problem = _work_cache(rest)
        if problem:
            return _out(problem, 2)
        return _out({"cache": cache.directory, "pending": cache.pending(),
                     # Only roots whose work a SEND can use: work at the
                     # receive threshold alone does not make an account able
                     # to pay, so listing it here would overstate the cache.
                     "have_work_for": [root for root in cache.roots()
                                       if cache.has(root, _work.SEND_THRESHOLD)]})

    if sub == "precompute":
        cache, problem = _work_cache(rest)
        if problem:
            return _out(problem, 2)
        names = {"send": _work.SEND_THRESHOLD, "receive": _work.RECEIVE_THRESHOLD}
        asked = _flag(rest, "threshold", str, "send")
        if asked not in names:
            return _out({"error": "unknown_threshold", "message":
                         "--threshold is send or receive, not %r" % asked}, 2)
        roots = _positional(rest)
        if len(roots) > 1:
            return _out({"error": "usage: work precompute [<root_hex>]"}, 2)
        try:
            done = cache.precompute(
                roots[0] if roots else None,
                threshold=names[asked],
                budget_seconds=_flag(rest, "budget", float),
                workers=_flag(rest, "workers", int))
        except prework.CacheUnwritable as exc:
            return _out({"error": "cache_unwritable", "message": str(exc)}, 2)
        except prework.CacheError as exc:
            return _out({"error": "bad_root", "message": str(exc)}, 2)
        except _work.WorkUnavailable as exc:
            return _out({"error": "work_unavailable", "message": str(exc)}, 1)
        done["cache"] = cache.directory
        return _out(done)

    return _out({"error": "unknown work subcommand %r" % sub}, 2)


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

        if command == "work":
            return _work_command(args)

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
