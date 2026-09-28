# nano-wallet-xno — audit, 2026-09-27

First audit of this repository. It is the one audited repository that is a
**published PyPI package** and a **wallet**, so it was audited as an artefact
someone installs, not only as a tree: the wheel was built, installed into a
clean virtualenv, and driven from the console script.

Read in full: `cli.py`, `keystore.py`, `wallet.py`, `nanoaddr.py`,
`ed25519_blake2b.py`, `pyproject.toml`, the README, and the test bootstrap.

## What was checked, and how

Baseline before any change, all green: 82 unit tests (`pytest` and
`unittest discover` agree), `e2e_check.py` 15/15, `e2e_receive_only.py` 16/16.

**Secrets, tree and full history.** Every commit reachable from every ref,
scanned for 64-hex values, token-shaped assignments and provider key prefixes.
Five 64-hex constants appear, and every one is a published test vector or a
value anybody can re-derive: the Nano **genesis public key**, RFC 8032 Ed25519
vector 1 (its secret and public key — the published vector, not a wallet), and
the private/public pair for the **all-zero seed**. No seed, no token, no
credential, in the tree or in history.

**The package builds and installs.** `python3 -m build --wheel` produces
`nano_wallet_xno-1.1.0-py3-none-any.whl` carrying exactly the fourteen modules
`[tool.setuptools] py-modules` names; `__main__.py`, `e2e_check.py` and
`e2e_receive_only.py` are the three top-level files left out, which is right —
they are repo-only. Installed into a clean venv, the `nano-wallet` console
script runs.

**`requires-python = ">=3.8"` is honest.** Every shipped module was scanned for
post-3.8 syntax and library surface — `str.removeprefix`/`removesuffix`, the
`match` statement, builtin generics (`list[str]`) in runtime position, PEP 604
`X | None`, `functools.cache`, `math.lcm`, `int.bit_count`, `zoneinfo`,
`graphlib`, `ast.unparse` — and none appears. No module relies on
`from __future__ import annotations` to get away with anything.

**The cryptography is right, checked against facts this repository cannot
influence.** Independently of the repo's own fixtures:

- the mainnet **genesis** public key encodes to the genesis address, character
  for character, and decodes back;
- the all-zero public key encodes to the canonical burn address;
- two accounts this swarm actually holds on mainnet
  (`nano_1434j1n4…`, `nano_3di779z3…`) validate and round-trip through
  `encode(decode(a))`;
- a one-character alteration of the genesis address is refused;
- the `xrb_` spelling normalises back to the same `nano_` address;
- **seed derivation is the Nano standard** — `blake2b(seed || index_be32, 32)`
  — verified at indices 0, 1, 7 and 2^32-1 by computing the expected key
  independently and comparing the resulting address;
- the curve is the **BLAKE2b** variant, not SHA-512: fed RFC 8032 vector 1's
  secret key it returns a different public key than the SHA-512 vector, which is
  correct for Nano and is what makes the RFC vector a meaningful control.

**The custody claim is verifiable from the installed artefact**, which is the
whole point of the package. `nano-wallet selfcheck` in the clean venv reports
7/7: the registered tools are `create_address, validate_address, balance,
receive`; `send` is **absent** from `tools/list` rather than disabled; a new
address is created with no network call; and no private key appears in any
output field.

**`keystore.py` holds the line it claims.** A key file is created with
`os.open(..., O_CREAT | O_EXCL, 0o600)`, so there is no window in which it
exists more permissively; the mode is then re-read and the file is **deleted
unused** if anything (a umask, a filesystem that ignores modes) left group or
other bits set, rather than reporting success. An existing key file is never
overwritten. Nothing in the module returns a key through a serialisable value.

## Fixed

**`--help` told every new user to run a command that does not exist**
(`cli.py:1`). The module docstring — which `main` prints verbatim for
`--help`, `-h`, `help` and a bare invocation — opened with:

```
Command line entry point: python3 -m nano_wallet <command>
```

There is no `nano_wallet` module. The package installs its modules flat
(`cli`, `wallet`, `nanoaddr`, …) and the entry point a user actually gets is the
`nano-wallet` console script from `[project.scripts]`. So the first line of the
first thing a new install prints was wrong, and copying it gives:

```
$ python3 -m nano_wallet check nano_1111…hifc8npp
/…/venv/bin/python: No module named nano_wallet
```

confirmed against the built wheel installed in a clean virtualenv, where
`importlib.util.find_spec("nano_wallet")` is `None`. The README had it right
throughout (`nano-wallet selfcheck …`), so the docstring contradicted the
documentation as well as the artefact.

The first line now reads `Command line entry point: nano-wallet <command>`.
Nothing else in the file changed.

Pinned by two laws in `tests/test_nano_wallet.py`:

- `test_every_module_the_help_offers_is_importable` captures the real help
  output, pulls every `python3 -m X` out of it, and requires each `X` to be
  importable. Fails before the fix with `SUBFAILED(module='nano_wallet')`.
- `test_the_help_names_the_console_script_pyproject_installs` reads the script
  name out of `[project.scripts]` rather than restating it, so renaming the
  script without updating the help turns this red. Fails before the fix with
  `AssertionError: 'nano-wallet' not found in 'Command line entry point:
  python3 -m nano_wallet <command>'`. It parses the TOML with a regex rather
  than `tomllib`, because this package supports Python 3.8 and `tomllib` is 3.11.

`README.md:190` said `Ran 82 tests — OK`; the two new laws make it 84, so that
line was updated in the same commit. Leaving it would have introduced into this
repository the exact defect fixed in `nano-receipt-ledger` the same day.

After the change: 84 unit tests, `e2e_check.py` 15/15,
`e2e_receive_only.py` 16/16, and a rebuilt wheel reinstalled in a fresh venv
prints `Command line entry point: nano-wallet <command>` and runs.

## Not merged, deliberately

This pull request is **left open for the owner** rather than merged. The audit
mandate permits self-merging only when a change touches no money code, and this
repository is a wallet: `cli.py` is the command surface that reaches seed
derivation. The change itself is one line of a docstring plus two tests and
touches no key path, no credential and no release workflow — but "it is only a
docstring, in a wallet" is exactly the judgement an auditor should not make
about its own patch.

## Found, not fixed

**The package installs fourteen modules under very generic top-level names** —
`wallet`, `cli`, `payments`, `blocks`, `keystore`, `profiles`, `capabilities`,
`selfcheck`, `nanonode`, `fakenode`, `wellknown`. Any of those can collide with
another distribution in the same environment, in either direction: something
else importing `cli` may get this package's, and this package's
`import wallet` may get something else's. Nothing is wrong today and the tests
pass, but it is the kind of thing that fails on a user's machine and not on
ours. The fix is a `nano_wallet/` package directory with the modules inside it,
which would also make the old `python3 -m nano_wallet` line correct — and that
is a restructuring of a published package with a version bump, not an audit
fix, so it is the owner's call. If it is ever done, `[project.scripts]` and the
MCP path in `README.md:69` move with it.

**`README.md:69`** gives an MCP client
`"/absolute/path/to/nano_wallet/mcp_server.py"`. No install lays the file out
that way: in a checkout it is `mcp_server.py` at the root, and installed it is
in `site-packages`. It reads as a placeholder, so it is unlikely to strand
anyone, but it names the same `nano_wallet/` directory that does not exist and
should be corrected with the decision above rather than separately.

**`cli._profile_arg` takes a `capabilities_default` parameter it never reads**,
and `cli.main` passes `capabilities_default=True` at one call site. Harmless,
and removing it is the kind of no-op rename this audit is told not to make.
