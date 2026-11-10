"""The published keygen line, held to its four claims.

    echo '<create_address store=file path=nano.key>' | python3 mcp_server.py --profile receive-only

  (a) the key goes into nano.key, readable only by your user (mode 0600)
  (b) the key is never printed, only the address - including on error paths
  (c) run again in the same folder, it refuses to overwrite the key
  (d) no network call

Every subprocess test runs that exact request through the real server, in a
fresh temporary directory, under an explicit umask. The error-path tests run
in-process so the operating-system calls can be made to fail.
"""

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mcp_server
import payments
import selfcheck

SERVER = os.path.join(ROOT, "mcp_server.py")
REQUEST = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "create_address",
                                 "arguments": {"store": "file", "path": "nano.key"}}})
HEX64 = re.compile(r"[0-9A-Fa-f]{64}")

#: Runs mcp_server.py as __main__ with every socket constructor and resolver
#: replaced by one that records the attempt on stderr and raises.
NO_SOCKET_LAUNCHER = r"""
import runpy, socket, sys
def refuse(*args, **kwargs):
    sys.stderr.write("SOCKET-ATTEMPTED\n")
    raise OSError("network refused by test")
class RefusedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        refuse()
socket.socket = RefusedSocket
socket.create_connection = refuse
socket.getaddrinfo = refuse
server = sys.argv[1]
sys.argv = [server, "--profile", "receive-only"]
sys.path.insert(0, __import__("os").path.dirname(server))
runpy.run_path(server, run_name="__main__")
"""


def _clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("NANO_")}


def run_line(cwd, umask=0o022, launcher=None):
    argv = ([sys.executable, "-c", launcher, SERVER] if launcher
            else [sys.executable, SERVER, "--profile", "receive-only"])
    out = subprocess.run(argv, input=REQUEST + "\n", capture_output=True, text=True,
                         cwd=cwd, env=_clean_env(), timeout=60,
                         preexec_fn=lambda: os.umask(umask))
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    response = json.loads(lines[-1]) if lines else None
    return out, response


def key_hex(path):
    with open(path, "r", encoding="ascii") as handle:
        return handle.read().strip().upper()


class PublishedLine(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.key = os.path.join(self.dir, "nano.key")

    def tearDown(self):
        self.tmp.cleanup()

    def assert_created(self, out, response):
        self.assertEqual(out.returncode, 0, out.stderr)
        result = response["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(stat.S_IMODE(os.lstat(self.key).st_mode), 0o600)
        self.assertTrue(stat.S_ISREG(os.lstat(self.key).st_mode))
        key = key_hex(self.key)
        self.assertEqual(len(key), 64)
        # (b) the address is printed, the key is not, in any case.
        self.assertIn(result["structuredContent"]["address"], out.stdout)
        for text in (out.stdout, out.stderr):
            self.assertNotIn(key, text.upper())
            for found in HEX64.findall(text):
                self.assertNotEqual(found.upper(), key)
        return key

    # -- (a) mode 0600 whatever the umask --------------------------------

    def test_mode_0600_under_umask_000(self):
        self.assert_created(*run_line(self.dir, umask=0o000))

    def test_mode_0600_under_umask_022(self):
        self.assert_created(*run_line(self.dir, umask=0o022))

    def test_mode_0600_under_umask_077(self):
        self.assert_created(*run_line(self.dir, umask=0o077))

    # -- (c) never overwritten -------------------------------------------

    def test_second_run_in_the_same_folder_refuses_and_keeps_the_key(self):
        first = self.assert_created(*run_line(self.dir))
        out, response = run_line(self.dir)
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "key_exists")
        self.assertEqual(key_hex(self.key), first)
        self.assertEqual(stat.S_IMODE(os.stat(self.key).st_mode), 0o600)
        self.assertNotIn(first, (out.stdout + out.stderr).upper())

    def _existing_0644(self, content):
        with open(self.key, "wb") as handle:
            handle.write(content)
        os.chmod(self.key, 0o644)
        before = os.stat(self.key)
        out, response = run_line(self.dir, umask=0o000)
        after = os.stat(self.key)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "key_exists")
        self.assertIn("refusing to overwrite", result["structuredContent"]["message"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        with open(self.key, "rb") as handle:
            self.assertEqual(handle.read(), content)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o644)    # not chmodded
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(after.st_ino, before.st_ino)
        if content.strip():
            self.assertNotIn(content.strip().decode().upper(), (out.stdout + out.stderr).upper())
        return result

    def test_existing_0644_key_file_is_refused_untouched_and_unread(self):
        self._existing_0644(("AB" * 32 + "\n").encode("ascii"))

    def test_existing_empty_0644_file_is_refused_untouched(self):
        self._existing_0644(b"")

    def test_symlink_to_a_file_is_refused_and_target_untouched(self):
        target = os.path.join(self.dir, "elsewhere")
        with open(target, "wb") as handle:
            handle.write(b"do not touch\n")
        os.chmod(target, 0o644)
        os.symlink(target, self.key)
        out, response = run_line(self.dir)
        self.assertEqual(response["result"]["structuredContent"]["error"], "key_exists")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"do not touch\n")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)
        self.assertTrue(os.path.islink(self.key))

    def test_dangling_symlink_is_refused_and_target_not_created(self):
        target = os.path.join(self.dir, "not-yet")
        os.symlink(target, self.key)
        out, response = run_line(self.dir)
        self.assertEqual(response["result"]["structuredContent"]["error"], "key_exists")
        self.assertFalse(os.path.lexists(target))
        self.assertTrue(os.path.islink(self.key))

    def test_directory_at_the_key_path_is_refused(self):
        os.mkdir(self.key)
        out, response = run_line(self.dir)
        self.assertEqual(response["result"]["structuredContent"]["error"], "key_exists")
        self.assertEqual(os.listdir(self.key), [])

    # -- (d) no network --------------------------------------------------

    def test_the_line_opens_no_socket(self):
        out, response = run_line(self.dir, launcher=NO_SOCKET_LAUNCHER)
        self.assertNotIn("SOCKET-ATTEMPTED", out.stderr)
        self.assert_created(out, response)


class ErrorPathsDoNotLeakTheKey(unittest.TestCase):
    """Force each OS call in the create path to fail; the key must appear nowhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = os.path.join(self.tmp.name, "nano.key")
        self.server = mcp_server.Server("receive-only", env={})
        self.generated = []
        real_create = payments.wallet.create_wallet

        def recording_create():
            account = real_create()
            self.generated.append(account["private_key"].upper())
            return account

        patcher = mock.patch.object(payments.wallet, "create_wallet", recording_create)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), selfcheck.no_sockets():
            response = self.server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "create_address",
                            "arguments": {"store": "file", "path": self.key}}})
        text = (json.dumps(response) + err.getvalue()).upper()
        self.assertEqual(len(self.generated), 1)
        key = self.generated[0]
        self.assertNotIn(key, text)
        for found in HEX64.findall(text):
            self.assertNotEqual(found, key)
        return response

    def assert_tool_error_and_nothing_left(self, response, reason):
        self.assertIn("result", response, response)
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], reason)
        self.assertFalse(os.path.lexists(self.key))

    def test_write_failure(self):
        with mock.patch("keystore.os.write", side_effect=OSError(5, "Input/output error")):
            response = self.call()
        self.assert_tool_error_and_nothing_left(response, "key_path_unwritable")

    def test_fsync_failure(self):
        with mock.patch("keystore.os.fsync", side_effect=OSError(5, "Input/output error")):
            response = self.call()
        self.assert_tool_error_and_nothing_left(response, "key_path_unwritable")

    def test_unexpected_exception_during_write(self):
        # Reaches the dispatcher's catch-all (-32603 with a one-frame traceback).
        with mock.patch("keystore.os.write", side_effect=RuntimeError("boom")):
            response = self.call()
        self.assertTrue("error" in response or response["result"]["isError"], response)

    def test_fchmod_failure_is_irrelevant(self):
        # The create path sets the mode in os.open and never chmods afterwards.
        with mock.patch("os.fchmod", side_effect=OSError(1, "Operation not permitted")), \
                mock.patch("os.chmod", side_effect=OSError(1, "Operation not permitted")):
            response = self.call()
        self.assertFalse(response["result"]["isError"], response)
        self.assertEqual(stat.S_IMODE(os.stat(self.key).st_mode), 0o600)

    def _after_write(self, fake_stat):
        real_stat, real_fsync = os.stat, os.fsync
        state = {"synced": False}

        def fsync(fd):
            real_fsync(fd)
            state["synced"] = True

        def patched_stat(path, *args, **kwargs):
            if state["synced"] and os.fspath(path) == self.key:
                return fake_stat(real_stat(path, *args, **kwargs))
            return real_stat(path, *args, **kwargs)

        with mock.patch("keystore.os.fsync", fsync), mock.patch("keystore.os.stat", patched_stat):
            return self.call()

    def test_filesystem_that_ignores_mode_bits_is_refused_and_key_deleted(self):
        def wide(real):
            fields = list(real)
            fields[0] = stat.S_IFREG | 0o644
            return os.stat_result(fields)
        response = self._after_write(wide)
        self.assert_tool_error_and_nothing_left(response, "key_permissions")
        self.assertIn("644", response["result"]["structuredContent"]["message"])

    def test_stat_failure_after_write_is_a_tool_error_and_leaves_no_key(self):
        def fail(real):
            raise OSError(5, "Input/output error")
        response = self._after_write(fail)
        self.assert_tool_error_and_nothing_left(response, "key_path_unwritable")


if __name__ == "__main__":
    unittest.main()
