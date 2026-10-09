"""create_address store=file when the path cannot be written.

A seller running the keygen line in a sandbox may have a read-only or
ephemeral working directory. The answer it gets must be one tool error that
names the step and the path - not a JSON-RPC -32603 carrying a traceback -
and nothing may be left on disk: no zero-length key, no half-written key.

These run as root too, where chmod 555 does not stop a write, so the
unwritable cases are made with a path under a regular file, or by having
the operating system call itself refuse.
"""

import errno
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keystore
import mcp_server
import payments


def create(server, path):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "create_address",
                                     "arguments": {"store": "file", "path": path}}})


class UnwritableKeyPath(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.server = mcp_server.Server("receive-only", env={})

    def tearDown(self):
        shutil.rmtree(self.directory)

    def assert_one_clear_error(self, response, step, path):
        self.assertNotIn("error", response, "a JSON-RPC error is a crash, not an answer")
        result = response["result"]
        self.assertTrue(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(payload["error"], "key_path_unwritable")
        self.assertEqual(payload["step"], step)
        self.assertEqual(payload["path"], path)
        self.assertIn(path, payload["message"])
        self.assertIn(step, payload["message"])
        self.assertNotIn("Traceback", json.dumps(response))
        self.assertNotIn("address", payload)
        return payload

    def test_parent_is_a_regular_file_is_one_error_naming_the_step(self):
        blocker = os.path.join(self.directory, "not-a-dir")
        open(blocker, "w").close()
        path = os.path.join(blocker, "nano.key")
        payload = self.assert_one_clear_error(create(self.server, path), "make_directory", path)
        self.assertIn("is not a directory", payload["message"])
        self.assertEqual(os.path.getsize(blocker), 0)

    def test_read_only_cwd_is_one_error_and_says_how_to_pass_a_path(self):
        refused = PermissionError(errno.EACCES, "Permission denied", "nano.key")
        cwd = os.getcwd()
        os.chdir(self.directory)
        try:
            with mock.patch.object(keystore.os, "open", side_effect=refused):
                payload = self.assert_one_clear_error(
                    create(self.server, "nano.key"), "create_key_file", "nano.key")
        finally:
            os.chdir(cwd)
        self.assertIn("Permission denied", payload["message"])
        self.assertIn(os.path.join(os.path.realpath(self.directory), "nano.key"),
                      payload["message"])
        self.assertIn("absolute", payload["message"])
        self.assertEqual(os.listdir(self.directory), [])

    def test_a_failed_write_leaves_no_zero_length_key(self):
        path = os.path.join(self.directory, "nano.key")
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(keystore.os, "write", side_effect=full):
            payload = self.assert_one_clear_error(
                create(self.server, path), "write_key_file", path)
        self.assertIn("No space left on device", payload["message"])
        self.assertFalse(os.path.exists(path))

    def test_a_short_write_is_completed_not_truncated(self):
        path = os.path.join(self.directory, "nano.key")
        real_write = os.write
        with mock.patch.object(keystore.os, "write",
                               side_effect=lambda fd, data: real_write(fd, data[:7])):
            created = payments.create_address(store="file", path=path,
                                              keys=keystore.KeyStore())
        self.assertEqual(os.path.getsize(path), 65)
        self.assertEqual(keystore.KeyStore().load_file(path), created["address"])

    def test_directories_it_creates_are_0700(self):
        path = os.path.join(self.directory, "new", "sub", "nano.key")
        payments.create_address(store="file", path=path, keys=keystore.KeyStore())
        for made in (os.path.join(self.directory, "new"),
                     os.path.join(self.directory, "new", "sub")):
            self.assertEqual(stat.S_IMODE(os.stat(made).st_mode), 0o700, made)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_existing_file_is_refused_and_unchanged(self):
        path = os.path.join(self.directory, "nano.key")
        payments.create_address(store="file", path=path, keys=keystore.KeyStore())
        with open(path, "rb") as handle:
            before = handle.read()
        response = create(self.server, path)
        self.assertEqual(response["result"]["structuredContent"]["error"], "key_exists")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
