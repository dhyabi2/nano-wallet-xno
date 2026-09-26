"""A Model Context Protocol server exposing the wallet as four tools.

Dependency-free: stdlib only, JSON-RPC 2.0 over newline-delimited stdio.
That is deliberate - "one install" has to mean one install. There is no
package to resolve, no service to reach and no account to open.

    {"mcpServers": {"nano-wallet": {"command": "python3",
                                    "args": ["/path/to/nano_wallet/mcp_server.py"]}}}

Tools:
    nano_validate_address   checksum-verify an address before anything uses it
    nano_create_wallet      generate a seed + account inside this process
    nano_derive_account     another account from a seed the caller holds
    nano_sign_message       prove control of an address without moving funds

There is no send and no balance tool here, and that is on purpose: this
server makes no network call at all, so an operator reviewing it has
nothing to weigh but four pure functions. Receiving needs nothing more.
"""

import json
import sys
import traceback

import wallet

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "nano-wallet", "version": "1.0.0"}

TOOLS = [
    {
        "name": "nano_validate_address",
        "description": (
            "Checksum-verify a Nano (XNO) account address. Returns valid=false with a "
            "machine-readable reason instead of throwing. Use this on every address "
            "before quoting it as a payout destination - a Nano address that fails its "
            "checksum looks entirely normal to the eye."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"address": {"type": "string", "description": "nano_... or xrb_... address"}},
            "required": ["address"],
        },
    },
    {
        "name": "nano_create_wallet",
        "description": (
            "Generate a new Nano seed and its first account inside this process. The seed "
            "and private key are returned to the caller and are not transmitted anywhere. "
            "Store the seed: losing it loses the funds."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
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
    {
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
]


def call_tool(name: str, arguments: dict) -> dict:
    arguments = arguments or {}
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
    raise KeyError("unknown tool %r" % name)


def handle(request: dict):
    """Return a JSON-RPC response dict, or None for a notification."""
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        try:
            payload = call_tool(params.get("name"), params.get("arguments"))
            result = {
                "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
                "structuredContent": payload,
                "isError": False,
            }
        except KeyError as exc:
            return _error(request_id, -32602, "unknown tool: %s" % exc)
        except (ValueError, wallet.InvalidAddress) as exc:
            result = {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        except Exception:                                   # pragma: no cover
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


def _error(request_id, code, message):
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None) -> None:
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
            response = handle(request)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
