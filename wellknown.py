"""Serves GET /.well-known/nano-receive-only, and nothing else.

The capability document has to be citable from a message - an outside
agent's operator wants a URL they can open themselves, not a paragraph
from us. This is the smallest thing that provides one.

The body is `capabilities.document()` verbatim, so the served document
and the one `nano-wallet capabilities` prints are the same object built
from the same tool tuple. The test suite asserts them byte for byte;
there is no second copy to drift.

Read-only: every method but GET and HEAD is 405, there is no route that
takes a body, and nothing here touches a key, a node or the filesystem.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import capabilities
import profiles

PATH = capabilities.WELL_KNOWN_PATH


class Handler(BaseHTTPRequestHandler):
    server_version = "nano-wallet-wellknown/1.1.0"
    profile = profiles.RECEIVE_ONLY

    def do_GET(self):                                       # noqa: N802 - stdlib name
        self._respond(body=True)

    def do_HEAD(self):                                      # noqa: N802 - stdlib name
        self._respond(body=False)

    def _respond(self, body: bool):
        if self.path.split("?", 1)[0] != PATH:
            return self._json(404, {"error": "not_found", "path": PATH}, body)
        document = capabilities.document(self.profile)
        return self._json(200, document, body)

    def _json(self, status: int, payload: dict, body: bool):
        blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        if body:
            self.wfile.write(blob)

    def log_message(self, fmt, *args):
        """Silent by default: this endpoint is public and its access log would
        be a list of who is considering paying us."""
        return


def make_server(host: str = "127.0.0.1", port: int = 8402,
                profile: str = profiles.RECEIVE_ONLY) -> HTTPServer:
    if profile not in profiles.PROFILE_TOOLS:
        raise capabilities.UnknownProfile(
            "unknown profile %r; known profiles: %s"
            % (profile, ", ".join(profiles.PROFILE_NAMES))
        )
    handler = type("BoundHandler", (Handler,), {"profile": profile})
    return HTTPServer((host, port), handler)


def main(argv=None) -> int:                                 # pragma: no cover - a loop
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    host, port = "127.0.0.1", 8402
    while argv:
        if argv[0] == "--host" and len(argv) > 1:
            host, argv = argv[1], argv[2:]
        elif argv[0] == "--port" and len(argv) > 1:
            port, argv = int(argv[1]), argv[2:]
        else:
            argv = argv[1:]
    server = make_server(host, port)
    sys.stderr.write("serving %s on http://%s:%d%s\n" % (PATH, host, port, PATH))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":                                  # pragma: no cover
    raise SystemExit(main())
