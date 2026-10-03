"""HttpNanoNode against answers shaped the way a real public node sends them.

The fake node in `fakenode.py` stands in for `NanoNode` itself, so it never
exercises how `HttpNanoNode` reads the node's JSON. That is where a brand-new
wallet broke: a real node answers `account_info` for an account that has
never received with `{"error": "Account not found"}`, and `_rpc` raised it
as `node_error` - so `balance` and `receive` failed for every new wallet,
the one state every new user starts in. No socket is opened here either:
`urlopen` is replaced for the duration of each test.
"""

import io
import json
import os
import sys
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nanonode  # noqa: E402

NEW = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _node_answering(answer, seen):
    def urlopen(request, timeout=None):
        seen.append(request)
        return _Answer(json.dumps(answer).encode("utf-8"))
    return urlopen


class HttpNodeAgainstRealAnswers(unittest.TestCase):
    def _node(self, answer):
        seen = []
        patch = mock.patch.object(urllib.request, "urlopen", _node_answering(answer, seen))
        patch.start()
        self.addCleanup(patch.stop)
        return nanonode.HttpNanoNode("https://user:key@node.example/rpc"), seen

    def test_a_never_opened_account_is_empty_not_an_error(self):
        node, _ = self._node({"error": "Account not found"})
        self.assertEqual(node.account_info(NEW), {})

    def test_other_node_errors_still_raise_without_credentials(self):
        node, _ = self._node({"error": "Bad account number"})
        with self.assertRaises(nanonode.NodeError) as caught:
            node.account_info(NEW)
        self.assertEqual(caught.exception.reason, "node_error")
        self.assertNotIn("key", caught.exception.message)

    def test_account_not_found_is_only_forgiven_for_account_info(self):
        node, _ = self._node({"error": "Account not found"})
        with self.assertRaises(nanonode.NodeError):
            node.process({"type": "state", "_subtype": "send"})

    def test_every_call_names_itself(self):
        # Cloudflare-fronted public nodes answer urllib's default
        # User-Agent with 403 "error code: 1010", indistinguishable from
        # a refused key.
        node, seen = self._node({"error": "Account not found"})
        node.account_info(NEW)
        agent = seen[0].get_header("User-agent") or ""
        self.assertTrue(agent.startswith("nano-wallet-xno/"), agent)


if __name__ == "__main__":
    unittest.main()
