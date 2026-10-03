"""A Model Context Protocol server exposing the wallet, under a profile.

Dependency-free: stdlib only, JSON-RPC 2.0 over newline-delimited stdio.
That is deliberate - "one install" has to mean one install. There is no
package to resolve, no service to reach and no account to open.

    {"mcpServers": {"nano-wallet": {"command": "npx",
                                    "args": ["-y", "nano-wallet-mcp",
                                             "--profile", "receive-only"],
                                    "env": {"NANO_WALLET_ALLOW_SEND": "0"}}}}

`--profile receive-only` registers exactly four tools:

    create_address      generate an address inside this process, offline
    validate_address    checksum-verify any address, offline
    balance             read a balance
    receive             pocket incoming XNO

`send` is NOT REGISTERED in that profile. It is absent from tools/list
and calling it is -32601 method not found, because the method genuinely
does not exist - not a 403, which would tell an operator the capability
is there and merely switched off. See profiles.py.
"""

import json
import os
import sys
import traceback

import keystore as _keystore
import nanonode
import payments
import profiles
import wallet

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "nano-wallet", "version": "1.1.0"}

_ADDRESS_ARG = {"type": "string", "description": "nano_... or xrb_... address"}

#: Every tool this package can define, keyed by the name a client calls. Which
#: of them are registered is decided by the profile, in profiles.py, and by
#: nothing else.
TOOL_DEFINITIONS = {
    "create_address": {
        "name": "create_address",
        "description": (
            "Generate a Nano (XNO) receiving address inside this process. Offline: no "
            "network call is made. The seed and private key are never returned, "
            "transmitted, logged or displayed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "maxLength": 64},
                "store": {"type": "string", "enum": ["memory", "file"], "default": "memory"},
                "path": {"type": "string", "description": "required if store=file"},
            },
        },
    },
    "validate_address": {
        "name": "validate_address",
        "description": (
            "Checksum-verify a Nano (XNO) address. Returns valid=false with a "
            "machine-readable reason instead of throwing. A Nano address that fails its "
            "checksum looks entirely normal to the eye and can never receive a payment."
        ),
        "inputSchema": {"type": "object", "properties": {"address": _ADDRESS_ARG},
                        "required": ["address"]},
    },
    "balance": {
        "name": "balance",
        "description": (
            "Read an account's balance and what is waiting to be pocketed. The balance is "
            "the account's latest block; `confirmed` says whether the network has confirmed "
            "that block yet."
        ),
        "inputSchema": {"type": "object", "properties": {"address": _ADDRESS_ARG},
                        "required": ["address"]},
    },
    "receive": {
        "name": "receive",
        "description": (
            "Pocket incoming XNO. Receiving is not spending: it moves nothing out of "
            "the account and passes through no spend gate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": _ADDRESS_ARG,
                "max_blocks": {"type": "integer", "minimum": 1, "maximum": 64, "default": 8},
            },
            "required": ["address"],
        },
    },
    "send": {
        "name": "send",
        "description": (
            "Send XNO. Refused with spend_not_enabled unless NANO_WALLET_ALLOW_SEND=1. "
            "When NANO_WALLET_MANDATE names an operator-signed mandate, every send must also "
            "fit its cap, per-payment max, payee allow-list and expiry (mandate_refused). "
            "Not registered at all under --profile receive-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": _ADDRESS_ARG,
                "to": _ADDRESS_ARG,
                "amount_xno": {"type": "string", "description": "decimal string, never a float"},
                "idempotency_key": {"type": "string", "maxLength": 64},
            },
            "required": ["from", "to", "amount_xno", "idempotency_key"],
        },
    },
    "nano_validate_address": {
        "name": "nano_validate_address",
        "description": (
            "Checksum-verify a Nano (XNO) account address. Returns valid=false with a "
            "machine-readable reason instead of throwing. Use this on every address "
            "before quoting it as a payout destination - a Nano address that fails its "
            "checksum looks entirely normal to the eye."
        ),
        "inputSchema": {"type": "object", "properties": {"address": _ADDRESS_ARG},
                        "required": ["address"]},
    },
    "nano_create_wallet": {
        "name": "nano_create_wallet",
        "description": (
            "Generate a new Nano seed and its first account inside this process. The seed "
            "and private key are returned to the caller and are not transmitted anywhere. "
            "Store the seed: losing it loses the funds."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    "nano_derive_account": {
        "name": "nano_derive_account",
        "description": "Derive account <index> from a 64-hex-character Nano seed the caller already holds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seed": {"type": "string", "description": "64 hex characters"},
                "index": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["seed"],
        },
    },
    "nano_sign_message": {
        "name": "nano_sign_message",
        "description": (
            "Sign a UTF-8 message with an account's private key, proving control of the "
            "corresponding address without revealing the key or moving any funds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "private_key": {"type": "string", "description": "64 hex characters"},
            },
            "required": ["message", "private_key"],
        },
    },
}


class Server:
    """One MCP server under one profile.

    Instantiating it is the startup gate: an impossible configuration
    raises profiles.ProfileError here, before anything is served.
    """

    def __init__(self, profile=None, env=None, node=None, keys=None):
        self.env = os.environ if env is None else env
        self.profile = profiles.resolve(profile, self.env)
        self.keys = keys if keys is not None else _keystore.KeyStore()
        self._node = node
        self._sent = {}
        key_file = self.env.get("NANO_WALLET_KEY_FILE")
        if key_file:
            self.keys.load_file(key_file)

    # -- surface ---------------------------------------------------------

    def tool_names(self) -> list:
        return list(profiles.tools_for(self.profile))

    def tools(self) -> list:
        return [TOOL_DEFINITIONS[name] for name in self.tool_names()]

    def node(self) -> nanonode.NanoNode:
        if self._node is None:
            self._node = nanonode.HttpNanoNode(self.env.get("NANO_NODE_URL", ""))
        return self._node

    # -- dispatch --------------------------------------------------------

    def call_tool(self, name: str, arguments: dict):
        arguments = arguments or {}
        if name not in self.tool_names():
            # Absent, not forbidden. An unregistered tool is -32601, the same
            # answer a client gets for a method that was never defined.
            raise NotRegistered(name, self.profile)

        if name == "create_address":
            return payments.create_address(
                label=arguments.get("label"),
                store=arguments.get("store", "memory"),
                path=arguments.get("path"),
                keys=self.keys,
            )
        if name == "validate_address":
            return payments.validate_address(arguments.get("address", ""))
        if name == "balance":
            return payments.balance(arguments.get("address", ""), self.node())
        if name == "receive":
            return payments.receive(arguments.get("address", ""), self.node(), self.keys,
                                    arguments.get("max_blocks", 8))
        if name == "send":
            return payments.send(
                arguments.get("from", ""), arguments.get("to", ""),
                arguments.get("amount_xno", ""), arguments.get("idempotency_key", ""),
                self.node(), self.keys, self._sent, self.env,
            )
        if name == "nano_validate_address":
            return wallet.validate(arguments.get("address", ""))
        if name == "nano_create_wallet":
            return wallet.create_wallet()
        if name == "nano_derive_account":
            return wallet.derive_account(arguments["seed"], int(arguments.get("index", 0)))
        if name == "nano_sign_message":
            return wallet.sign_message(
                str(arguments["message"]).encode("utf-8"), arguments["private_key"]
            )
        raise NotRegistered(name, self.profile)              # pragma: no cover - defensive

    # -- protocol --------------------------------------------------------

    def handle(self, request: dict):
        """Return a JSON-RPC response dict, or None for a notification."""
        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": dict(SERVER_INFO, profile=self.profile),
            }
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                payload = self.call_tool(params.get("name"), params.get("arguments"))
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
                    "structuredContent": payload,
                    "isError": False,
                }
            except NotRegistered as exc:
                return _error(request_id, -32601, str(exc))
            except payments.ToolError as exc:
                payload = exc.payload()
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
                    "structuredContent": payload,
                    "isError": True,
                }
            except (ValueError, wallet.InvalidAddress, _keystore.KeyStoreError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except Exception:                                # pragma: no cover
                return _error(request_id, -32603, traceback.format_exc(limit=1))
        elif method in ("notifications/initialized", "initialized"):
            return None
        elif method == "ping":
            result = {}
        else:
            return _error(request_id, -32601, "method not found: %r" % method)

        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "parse error"}}
            else:
                response = self.handle(request)
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()


class NotRegistered(LookupError):
    """A tool that does not exist in this profile. Answered as -32601."""

    def __init__(self, name, profile):
        super().__init__(
            "method not found: tool %r is not registered in the %r profile"
            % (name, profile)
        )
        self.name = name
        self.profile = profile


def _error(request_id, code, message):
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# -- module-level shims, so nano-wallet 1.0.0 callers keep working ---------

#: The tool definitions of the default profile, in registration order. Kept as a
#: module attribute because nano-wallet 1.0.0 exposed one.
TOOLS = [TOOL_DEFINITIONS[name] for name in profiles.tools_for(profiles.DEFAULT_PROFILE)]


def handle(request: dict):
    return Server().handle(request)


def serve(stdin=None, stdout=None) -> None:
    Server().serve(stdin, stdout)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    profile = None
    while argv:
        if argv[0] == "--profile" and len(argv) > 1:
            profile, argv = argv[1], argv[2:]
        elif argv[0].startswith("--profile="):
            profile, argv = argv[0].split("=", 1)[1], argv[1:]
        else:
            argv = argv[1:]
    try:
        server = Server(profile)
    except profiles.ProfileError as exc:
        # Exit BEFORE serving anything. A profile a stray environment variable
        # can widen is not a profile.
        sys.stderr.write(exc.message + "\n")
        return exc.exit_code
    except _keystore.KeyStoreError as exc:
        sys.stderr.write(exc.message + "\n")
        return 2
    sys.stderr.write("nano-wallet %s: profile %s, tools: %s\n"
                     % (SERVER_INFO["version"], server.profile,
                        ", ".join(server.tool_names())))
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
