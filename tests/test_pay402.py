"""nano-pay402: dry-run by default, refuses before it signs, never pays twice.

No test here opens a socket except the redirect law (127.0.0.1 only); the seller is a
function, the node is fakenode.FakeNode, and no real payment is possible.
"""
import base64
import contextlib
import io
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ed25519_blake2b as ed
import fakenode
import keystore
import nanoaddr
import payments
import pay402

RAW = 10 ** 30
SELLER_KEY = bytes.fromhex("33" * 32)
SELLER = nanoaddr.encode(ed.public_key_from_private(SELLER_KEY))
PRICE = 5 * 10 ** 28            # 0.05 XNO
URL = "https://seller.example/api/audit"
NONCE = "7fffffff.00112233445566778899aabbccddeeff"
# Same vector as dual-rail's tests/test_binding.py: both repos must produce this signature.
VECTOR_PAY_TO = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
VECTOR_SIGNATURE = ("4FD7E244EB8A7C4ABB8B1E8AF1A232B02BB696E28729048F89AB825883C479DA"
                    "0B166E755AE8DF2F94F85A3C962E28254629F50553C9D54B633999511FC52E05")


def entry(**kw):
    e = {"scheme": "exact", "network": "nano:mainnet", "asset": "XNO", "payTo": SELLER,
         "amount": str(PRICE), "maxAmountRequired": str(PRICE)}
    e.update(kw)
    return e


def challenge(*entries):
    return {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "base", "asset": "USDC",
                                           "amount": "10000", "payTo": "0xabc"}] + list(entries)}


class Seller:
    """A fetcher: 402 until a payment header arrives, then `answer`."""

    def __init__(self, doc, answer=(200, b'{"ok": true}')):
        self.doc, self.answer, self.requests = doc, list(answer) if isinstance(answer, list) else [answer], []

    def __call__(self, url, method="GET", data=None, headers=None, timeout=20):
        self.requests.append((url, dict(headers or {})))
        if "X-PAYMENT" not in (headers or {}):
            return 402, {}, json.dumps(self.doc).encode()
        status, body = self.answer.pop(0) if len(self.answer) > 1 else self.answer[0]
        return status, {}, body


def run(argv, fetcher, node=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = pay402.main(argv, fetcher=fetcher, node=node, sleep=lambda s: None)
    return code, json.loads(buf.getvalue())


class Funded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.node = fakenode.FakeNode()
        keys = keystore.KeyStore()
        self.key_file = os.path.join(self.tmp.name, "key")
        created = payments.create_address(store="file", path=self.key_file, keys=keys)
        self.payer = created["address"]
        self.node.fund(self.payer, 10 * RAW)
        payments.receive(self.payer, self.node, keys)

    def sends(self):
        return [b for b in self.node.published if b.get("_subtype") == "send" or
                (b["link"] == nanoaddr.decode(SELLER).hex().upper())]


class DryRun(unittest.TestCase):
    def test_dry_run_is_the_default_and_touches_no_key_and_no_node(self):
        seller = Seller(challenge(entry()))
        code, out = run([URL], seller)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        self.assertEqual((out["pay_to"], out["amount_raw"], out["amount_xno"]), (SELLER, str(PRICE), "0.05"))
        self.assertEqual(len(seller.requests), 1)
        self.assertNotIn("X-PAYMENT", seller.requests[0][1])

    def test_refusals_before_anything_is_signed(self):
        bad_addr = SELLER[:-1] + ("1" if SELLER[-1] != "1" else "3")
        cases = {
            "no_nano_entry": challenge(),
            "bad_pay_to": challenge(entry(payTo=bad_addr)),
            "bad_amount": challenge(entry(amount="0.05", maxAmountRequired="0.05")),
            "binding_scheme_unsupported": challenge(entry(extra={"binding": {"scheme": "other/9"}})),
            "binding_resource_mismatch": challenge(entry(extra={"binding": {
                "scheme": "nano-block-sig/1", "nonce": NONCE, "resource": "/elsewhere"}})),
        }
        for reason, doc in cases.items():
            code, out = run([URL], Seller(doc))
            self.assertEqual((code, out["refused"]), (1, reason), reason)

    def test_over_cap_and_non_402(self):
        self.assertEqual(run([URL, "--max-xno", "0.01"], Seller(challenge(entry())))[1]["refused"], "over_cap")
        self.assertEqual(run([URL, "--max-xno", "0.05"], Seller(challenge(entry())))[0], 0)
        self.assertEqual(run([URL, "--max-xno", "abc"], Seller(challenge(entry())))[1]["refused"], "bad_cap")
        code, out = run([URL], lambda *a, **k: (200, {}, b"hi"))
        self.assertEqual(out["refused"], "not_a_402")

    def test_paying_requires_a_cap_and_a_key(self):
        seller = Seller(challenge(entry()))
        self.assertEqual(run([URL, "--pay"], seller)[1]["refused"], "cap_required")
        self.assertEqual(run([URL, "--pay", "--max-xno", "1"], seller)[1]["refused"], "key_required")
        self.assertEqual(len(seller.requests), 2, "nothing but the 402 fetches happened")

    def test_v2_payment_required_header_is_read(self):
        header = base64.b64encode(json.dumps(challenge(entry())).encode()).decode()
        code, out = run([URL], lambda *a, **k: (402, {"payment-required": header}, b""))
        self.assertEqual(code, 0)
        self.assertEqual(out["pay_to"], SELLER)

    def test_a_redirect_is_not_followed(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/other")
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        status, _, _ = pay402.fetch("http://127.0.0.1:%d/x" % server.server_port)
        self.assertEqual(status, 302)


class Paying(Funded):
    def pay(self, seller, extra=()):
        return run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1", *extra],
                   seller, self.node)

    def test_pays_exactly_the_quote_and_presents_the_block(self):
        seller = Seller(challenge(entry()))
        code, out = self.pay(seller)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["paid"] and out["confirmed"])
        block = [b for b in self.node.published if _hash(b) == out["block_hash"]][0]
        self.assertEqual(block["link"], nanoaddr.decode(SELLER).hex().upper())
        self.assertEqual(int(self.node.accounts[self.payer]["balance_raw"]), 10 * RAW - PRICE)
        header = seller.requests[-1][1]["X-PAYMENT"]
        doc = json.loads(base64.b64decode(header))
        self.assertEqual(doc["payload"]["blockHash"], out["block_hash"])
        self.assertEqual(doc["network"], "nano:mainnet")
        self.assertNotIn("binding", doc["payload"])
        private = open(self.key_file).read().strip()
        self.assertNotIn(private, json.dumps(out))

    def test_binding_is_signed_by_the_paying_key(self):
        e = entry(extra={"binding": {"scheme": "nano-block-sig/1", "nonce": NONCE, "resource": "/api/audit"}})
        seller = Seller(challenge(e))
        code, out = self.pay(seller)
        self.assertEqual(code, 0, out)
        doc = json.loads(base64.b64decode(seller.requests[-1][1]["X-PAYMENT"]))
        sig = bytes.fromhex(doc["payload"]["binding"]["signature"])
        msg = pay402.binding_message(out["block_hash"], NONCE, "/api/audit", SELLER)
        self.assertTrue(ed.verify(msg, sig, nanoaddr.decode(self.payer)) if hasattr(ed, "verify")
                        else sig == ed.sign(msg, bytes.fromhex(open(self.key_file).read().strip()),
                                            nanoaddr.decode(self.payer)))

    def test_cross_repo_vector(self):
        priv = bytes.fromhex("11" * 32)
        self.assertEqual(pay402.binding_signature(priv, "AB" * 32, NONCE, "/report", VECTOR_PAY_TO),
                         VECTOR_SIGNATURE)

    def test_a_failed_retry_never_pays_twice(self):
        seller = Seller(challenge(entry()), answer=[(500, b"oops")])
        code, out = self.pay(seller)
        self.assertEqual(code, 3)
        first = out["block_hash"]
        seller2 = Seller(challenge(entry()), answer=[(200, b"ok")])
        code, out = self.pay(seller2)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["block_hash"], first)
        self.assertTrue(out["replayed_send"])
        paid = [b for b in self.node.published if b["link"] == nanoaddr.decode(SELLER).hex().upper()]
        self.assertEqual(len(paid), 1)

    def test_the_cap_binds_the_send_itself(self):
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.01"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual(out["refused"], "over_cap")
        self.assertEqual([b for b in self.node.published if b["link"] == nanoaddr.decode(SELLER).hex().upper()], [])

    def test_ledger_file_is_private(self):
        self.pay(Seller(challenge(entry())))
        mode = stat.S_IMODE(os.stat(self.key_file + ".pay402-sent.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_insufficient_balance_is_refused_not_crashed(self):
        e = entry(amount=str(50 * RAW), maxAmountRequired=str(50 * RAW))
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "100"],
                        Seller(challenge(e)), self.node)
        self.assertEqual((code, out["refused"]), (1, "insufficient_balance"))


OTHER_SELLER = nanoaddr.encode(ed.public_key_from_private(bytes.fromhex("44" * 32)))


class PinPayTo(Funded):
    """--pin-file: a seller's origin keeps the payTo it named on first read."""

    def setUp(self):
        super().setUp()
        self.pins = os.path.join(self.tmp.name, "pins.json")

    def read(self, pay_to, url=URL, pins=True):
        return run([url, "--max-xno", "0.1"] + (["--pin-file", self.pins] if pins else []),
                   Seller(challenge(entry(payTo=pay_to))))

    def test_first_read_pins_the_pay_to(self):
        code, out = self.read(SELLER)
        self.assertEqual(code, 0, out)
        self.assertEqual((out["pin"]["origin"], out["pin"]["pay_to"], out["pin"]["pinned_now"]),
                         ("https://seller.example:443", SELLER, True))
        with open(self.pins) as handle:
            stored = json.load(handle)
        self.assertEqual(stored["https://seller.example:443"]["pay_to"], SELLER)
        self.assertIn("first_seen", stored["https://seller.example:443"])
        self.assertEqual(stat.S_IMODE(os.stat(self.pins).st_mode), 0o600)

    def test_same_pay_to_passes(self):
        self.read(SELLER)
        code, out = self.read(SELLER, url="https://SELLER.example:443/api/other")
        self.assertEqual(code, 0, out)
        self.assertFalse(out["pin"]["pinned_now"])

    def test_changed_pay_to_is_refused(self):
        self.read(SELLER)
        code, out = self.read(OTHER_SELLER, url="https://seller.example/api/other")
        self.assertEqual((code, out["refused"]), (1, "payto-changed"), out)
        self.assertIn(SELLER, out["message"])
        self.assertIn(OTHER_SELLER, out["message"])
        with open(self.pins) as handle:
            self.assertEqual(json.load(handle)["https://seller.example:443"]["pay_to"], SELLER,
                             "a refused read does not move the pin")

    def test_changed_pay_to_is_refused_before_paying(self):
        self.read(SELLER)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1",
                         "--pin-file", self.pins], Seller(challenge(entry(payTo=OTHER_SELLER))), self.node)
        self.assertEqual((code, out["refused"]), (1, "payto-changed"), out)
        self.assertEqual([b for b in self.node.published
                          if b["link"] == nanoaddr.decode(OTHER_SELLER).hex().upper()], [])

    def test_without_pin_file_nothing_is_compared(self):
        self.read(SELLER)
        code, out = self.read(OTHER_SELLER, pins=False)
        self.assertEqual(code, 0, out)
        self.assertNotIn("pin", out)

    def test_the_pin_is_per_origin(self):
        self.read(SELLER)
        for other in ("https://other.example/api/audit", "http://seller.example/api/audit",
                      "https://seller.example:8443/api/audit"):
            code, out = self.read(OTHER_SELLER, url=other)
            self.assertEqual(code, 0, (other, out))
            self.assertTrue(out["pin"]["pinned_now"], other)
        code, out = self.read(OTHER_SELLER)
        self.assertEqual(out["refused"], "payto-changed")


    # -- fail closed on a damaged pin file, never rewrite it -----------------------------

    def _refused_untouched(self, content, reason):
        with open(self.pins, "w") as handle:
            handle.write(content)
        code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, reason), out)
        with open(self.pins) as handle:
            self.assertEqual(handle.read(), content, "a refused pin file is not rewritten")

    def test_corrupt_pin_file_is_refused(self):
        self._refused_untouched("{not json", "pin-file-malformed")

    def test_truncated_pin_file_is_refused(self):
        full = json.dumps({"https://other.example:443": {"pay_to": OTHER_SELLER, "first_seen": "x"}})
        self._refused_untouched(full[: len(full) // 2], "pin-file-malformed")

    def test_list_top_level_is_refused(self):
        self._refused_untouched(json.dumps([{"pay_to": SELLER}]), "pin-file-malformed")

    def test_empty_pay_to_entry_is_refused(self):
        self._refused_untouched(json.dumps({"https://seller.example:443": {"pay_to": ""}}),
                                "pin-entry-malformed")

    def test_bare_string_entry_is_refused(self):
        self._refused_untouched(json.dumps({"https://seller.example:443": OTHER_SELLER}),
                                "pin-entry-malformed")

    def test_malformed_entry_of_another_origin_is_refused(self):
        self._refused_untouched(json.dumps({"https://other.example:443": 7}), "pin-entry-malformed")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any file")
    def test_unreadable_pin_file_is_refused(self):
        with open(self.pins, "w") as handle:
            json.dump({"https://other.example:443": {"pay_to": OTHER_SELLER}}, handle)
        os.chmod(self.pins, 0)
        self.addCleanup(os.chmod, self.pins, 0o600)
        code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, "pin-file-unreadable"), out)
        os.chmod(self.pins, 0o600)
        with open(self.pins) as handle:
            self.assertIn("https://other.example:443", json.load(handle))

    def test_permission_error_on_pin_file_is_refused(self):
        # The chmod test above is skipped as root; this one injects the same OSError.
        from unittest import mock
        content = json.dumps({"https://other.example:443": {"pay_to": OTHER_SELLER}})
        with open(self.pins, "w") as handle:
            handle.write(content)
        real_open = os.open

        def denying_open(path, *a, **kw):
            if path == self.pins:
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, *a, **kw)

        with mock.patch.object(pay402.os, "open", denying_open):
            code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, "pin-file-unreadable"), out)
        with open(self.pins) as handle:
            self.assertEqual(handle.read(), content)

    def test_directory_at_pin_path_is_refused_cleanly(self):
        os.mkdir(self.pins)
        code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, "pin-file-unreadable"), out)
        self.assertTrue(os.path.isdir(self.pins))

    def test_zero_byte_pin_file_says_empty_and_names_the_path(self):
        open(self.pins, "w").close()
        code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, "pin-file-malformed"), out)
        self.assertIn("empty", out["message"])
        self.assertIn("remove it", out["message"])
        self.assertIn(self.pins, out["message"])
        self.assertTrue(os.path.exists(self.pins), "an empty pin file is not auto-deleted")
        self.assertEqual(os.path.getsize(self.pins), 0)

    def test_pin_file_in_missing_directory_is_refused_and_leaves_nothing(self):
        missing = os.path.join(self.tmp.name, "no-such-dir")
        self.pins = os.path.join(missing, "pins.json")
        before = sorted(os.listdir(self.tmp.name))
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp.name)
        code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, "pin-file-unwritable"), out)
        self.assertIn("does not exist", out["message"])
        self.assertIn(missing, out["message"])
        self.assertFalse(os.path.exists(missing), "the directory is not created")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), before, "no stray .lock or temp file")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_fifo_at_pin_path_is_refused_without_hanging(self):
        os.mkfifo(self.pins)
        result = {}
        worker = threading.Thread(target=lambda: result.update(zip(("code", "out"), self.read(SELLER))),
                                  daemon=True)
        worker.start()
        worker.join(10)
        if worker.is_alive():
            # Unblock the hung reader so the test process can finish, then fail.
            with open(self.pins, "w"):
                pass
            worker.join(5)
            self.fail("a FIFO pin file blocked the read while holding FILE.lock")
        self.assertEqual((result["code"], result["out"].get("refused")), (1, "pin-file-unreadable"),
                         result)
        self.assertIn("not a regular file", result["out"]["message"])
        # The lock was released: a second check on another path through the same lock works.
        with pay402._file_lock(self.pins):
            pass

    def test_nul_in_pin_path_is_a_clean_refusal(self):
        with self.assertRaises(pay402.Refused) as caught:
            pay402.check_pin(self.pins + "\0x", URL, SELLER)
        self.assertEqual(caught.exception.reason, "pin-file-unreadable")

    def test_docs_do_not_blame_an_older_version_for_empty_pin_files(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("pay402.py", "README.md"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                text = " ".join(handle.read().split())
            self.assertNotIn("older version could leave", text, name)
            self.assertIn("empty pin file (e.g. after a crash) holds no pins", text, name)

    def test_bad_port_is_refused_cleanly(self):
        for url in ("https://seller.example:99999/api/audit", "https://seller.example:abc/api/audit"):
            code, out = self.read(SELLER, url=url)
            self.assertEqual((code, out.get("refused")), (2, "usage"), (url, out))
        self.assertFalse(os.path.exists(self.pins))

    def test_trailing_dot_host_is_the_same_origin(self):
        self.read(SELLER)
        code, out = self.read(OTHER_SELLER, url="https://seller.example./api/audit")
        self.assertEqual((code, out.get("refused")), (1, "payto-changed"), out)

    def test_concurrent_first_reads_keep_every_pin(self):
        origins = ["https://s%d.example" % i for i in range(24)]
        errors = []

        def one(origin):
            try:
                pay402.check_pin(self.pins, origin + "/x", SELLER)
            except Exception as exc:                         # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=one, args=(o,)) for o in origins]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(self.pins) as handle:
            stored = json.load(handle)
        self.assertEqual(sorted(stored), sorted(o + ":443" for o in origins))
        leftovers = [n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_empty_pin_file_value_is_a_usage_error(self):
        for value in ("", "   "):
            seller = Seller(challenge(entry()))
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1",
                             "--pin-file", value], seller, self.node)
            self.assertEqual((code, out.get("refused")), (2, "usage"), (value, out))
            self.assertIn("--pin-file", out["message"])
            self.assertEqual(seller.requests, [], "refused before the seller is even read")
        self.assertEqual(self.sends(), [])

    def test_entry_without_first_seen_is_refused(self):
        self._refused_untouched(json.dumps({"https://seller.example:443": {"pay_to": SELLER}}),
                                "pin-entry-malformed")

    def test_entry_of_another_origin_without_first_seen_is_refused(self):
        self._refused_untouched(json.dumps({"https://other.example:443": {"pay_to": OTHER_SELLER}}),
                                "pin-entry-malformed")

    def test_files_this_code_writes_still_load(self):
        self.read(SELLER)
        self.read(SELLER, url="https://other.example/x")
        code, out = self.read(SELLER)
        self.assertEqual(code, 0, out)
        self.assertFalse(out["pin"]["pinned_now"])

    def test_duplicate_origin_key_is_refused(self):
        content = ('{"https://seller.example:443": {"pay_to": "%s", "first_seen": "a"}, '
                   '"https://seller.example:443": {"pay_to": "%s", "first_seen": "b"}}'
                   % (OTHER_SELLER, SELLER))
        self._refused_untouched(content, "pin-file-malformed")

    def test_duplicate_inner_key_is_refused(self):
        content = ('{"https://seller.example:443": {"pay_to": "%s", "pay_to": "%s", '
                   '"first_seen": "a"}}' % (OTHER_SELLER, SELLER))
        self._refused_untouched(content, "pin-file-malformed")

    def test_dangling_symlink_at_pin_path_is_refused(self):
        os.symlink(os.path.join(self.tmp.name, "missing.json"), self.pins)
        code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, "pin-file-unreadable"), out)
        self.assertTrue(os.path.islink(self.pins))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "missing.json")))

    def test_live_symlink_at_pin_path_is_refused_and_kept(self):
        real = os.path.join(self.tmp.name, "real.json")
        content = json.dumps({"https://other.example:443": {"pay_to": OTHER_SELLER, "first_seen": "x"}})
        with open(real, "w") as handle:
            handle.write(content)
        os.symlink(real, self.pins)
        code, out = self.read(SELLER)
        self.assertEqual((code, out.get("refused")), (1, "pin-file-unreadable"), out)
        self.assertTrue(os.path.islink(self.pins), "the link is not replaced by a plain file")
        with open(real) as handle:
            self.assertEqual(handle.read(), content)

    def test_missing_plain_pin_path_is_first_use(self):
        self.assertFalse(os.path.lexists(self.pins))
        code, out = self.read(SELLER)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["pin"]["pinned_now"])

    def test_save_does_not_follow_a_planted_tmp_symlink(self):
        target = os.path.join(self.tmp.name, "victim")
        with open(target, "w") as handle:
            handle.write("keep")
        os.symlink(target, self.pins + ".tmp")
        code, out = self.read(SELLER)
        self.assertEqual(code, 0, out)
        with open(target) as handle:
            self.assertEqual(handle.read(), "keep")

    def _pay_refused(self, reason):
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1",
                         "--pin-file", self.pins], Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("refused")), (1, reason), out)
        self.assertEqual(len(self.node.published), before, "nothing is paid")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "needs O_NOFOLLOW")
    def test_symlink_swapped_in_after_the_islink_check_is_refused(self):
        real = os.path.join(self.tmp.name, "real.json")
        content = json.dumps({"https://seller.example:443": {"pay_to": SELLER, "first_seen": "x"}})
        with open(real, "w") as handle:
            handle.write(content)
        os.symlink(real, self.pins)
        # islink() said "plain file", then the path became a link: the open must not follow it.
        with mock.patch.object(pay402.os.path, "islink", return_value=False):
            self._pay_refused("pin-file-unreadable")
        self.assertTrue(os.path.islink(self.pins))
        with open(real) as handle:
            self.assertEqual(handle.read(), content)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and pay402.fcntl is not None, "needs O_NOFOLLOW and fcntl")
    def test_symlink_at_lock_path_is_refused(self):
        target = os.path.join(self.tmp.name, "victim")
        os.symlink(target, self.pins + ".lock")
        self._pay_refused("pin-file-unwritable")
        self.assertFalse(os.path.lexists(target), "the lock open does not follow the link")
        self.assertFalse(os.path.lexists(self.pins))

    def test_deeply_nested_pin_file_is_refused(self):
        self._refused_untouched("[" * 100000, "pin-file-malformed")
        self._pay_refused("pin-file-malformed")



class SentLedgerFailsClosed(Funded):
    def test_corrupt_sent_ledger_is_refused_before_signing(self):
        ledger = self.key_file + ".pay402-sent.json"
        with open(ledger, "w") as handle:
            handle.write("{trunc")
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("refused")), (1, "ledger-malformed"), out)
        self.assertEqual(len(self.node.published), before)
        with open(ledger) as handle:
            self.assertEqual(handle.read(), "{trunc")

    def test_corrupt_sent_ledger_message_never_says_remove(self):
        ledger = self.key_file + ".pay402-sent.json"
        for content in ("{trunc", "[]", ""):
            with open(ledger, "w") as handle:
                handle.write(content)
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
            self.assertEqual((code, out.get("refused")), (1, "ledger-malformed"), (content, out))
            message = out["message"]
            self.assertIn("do NOT delete it", message, content)
            self.assertIn("backup", message, content)
            self.assertNotIn("remove", message, content)
            self.assertNotIn("rm ", message, content)
            if not content:
                self.assertIn("empty", message)
            with open(ledger) as handle:
                self.assertEqual(handle.read(), content)

    def test_empty_sent_ledger_message_names_the_brace_recovery(self):
        ledger = self.key_file + ".pay402-sent.json"
        for content in ("", "  \n\t"):
            with open(ledger, "w") as handle:
                handle.write(content)
            before = len(self.node.published)
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
            self.assertEqual((code, out.get("refused")), (1, "ledger-malformed"), (content, out))
            self.assertEqual(len(self.node.published), before)
            message = out["message"]
            self.assertIn("write {} into it", message, content)
            self.assertIn("sure no payment was recorded", message, content)
            self.assertIn(ledger, message, content)
            self.assertIn("do NOT delete it", message, content)
            with open(ledger) as handle:
                self.assertEqual(handle.read(), content, "the ledger is never rewritten for you")

    def test_non_empty_corrupt_sent_ledger_does_not_suggest_braces(self):
        ledger = self.key_file + ".pay402-sent.json"
        for content in ("{trunc", "[]"):
            with open(ledger, "w") as handle:
                handle.write(content)
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
            self.assertEqual(out.get("refused"), "ledger-malformed", out)
            self.assertNotIn("{}", out["message"], content)
            self.assertIn("do NOT delete it", out["message"], content)

    def test_readme_flags_the_sent_ledger_behaviour_change(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "README.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("### Upgrading: a damaged sent ledger now stops --pay", text)
        self.assertIn("even without `--pin-file`", text)
        self.assertIn("write `{}` into it", text)

    def test_duplicate_key_in_sent_ledger_is_refused(self):
        ledger = self.key_file + ".pay402-sent.json"
        content = '{"k": "a", "k": "b"}'
        with open(ledger, "w") as handle:
            handle.write(content)
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("refused")), (1, "ledger-malformed"), out)
        self.assertEqual(len(self.node.published), before)

    def test_deeply_nested_sent_ledger_is_refused(self):
        ledger = self.key_file + ".pay402-sent.json"
        content = "[" * 100000
        with open(ledger, "w") as handle:
            handle.write(content)
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("refused")), (1, "ledger-malformed"), out)
        self.assertEqual(len(self.node.published), before)
        with open(ledger) as handle:
            self.assertEqual(handle.read(), content)

    def test_sent_ledger_entry_that_is_not_an_object_is_refused(self):
        ledger = self.key_file + ".pay402-sent.json"
        for content in ('{"a": 1}', '{"a": []}', '{"a": "x"}', '{"a": null}'):
            with open(ledger, "w") as handle:
                handle.write(content)
            before = len(self.node.published)
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
            self.assertEqual((code, out.get("refused")), (1, "ledger-malformed"), (content, out))
            self.assertEqual(len(self.node.published), before, content)
            self.assertIn(ledger, out["message"], content)
            self.assertIn("do NOT delete it", out["message"], content)
            with open(ledger) as handle:
                self.assertEqual(handle.read(), content)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_dangling_symlink_sent_ledger_is_refused_before_signing(self):
        ledger = self.key_file + ".pay402-sent.json"
        target = os.path.join(os.path.dirname(ledger), "gone.json")
        os.symlink(target, ledger)
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("refused")), (1, "ledger-unreadable"), out)
        self.assertEqual(len(self.node.published), before, "nothing is signed or published")
        self.assertIn(ledger, out["message"])
        self.assertIn("dangling", out["message"])
        self.assertTrue(os.path.islink(ledger), "the link is left as it was")
        self.assertFalse(os.path.lexists(target))

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_symlink_to_a_valid_sent_ledger_is_still_read(self):
        ledger = self.key_file + ".pay402-sent.json"
        real = os.path.join(os.path.dirname(ledger), "real-ledger.json")
        with open(real, "w") as handle:
            handle.write("{}")
        os.symlink(real, ledger)
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid")), (0, True), out)
        self.assertEqual(len(self.node.published), before + 1)

    def test_missing_sent_ledger_on_first_use_still_pays(self):
        ledger = self.key_file + ".pay402-sent.json"
        self.assertFalse(os.path.lexists(ledger))
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid")), (0, True), out)
        self.assertEqual(len(self.node.published), before + 1)
        with open(ledger) as handle:
            self.assertEqual(len(json.load(handle)), 1)

    def test_sent_ledger_in_a_missing_directory_is_refused_before_signing(self):
        base = os.path.dirname(self.key_file)
        plain = os.path.join(base, "plain-file")
        with open(plain, "w") as handle:
            handle.write("x")
        for ledger in (os.path.join(base, "no-such-dir", "sent.json"),
                       os.path.join(plain, "sent.json")):
            before = len(self.node.published)
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1",
                             "--ledger", ledger], Seller(challenge(entry())), self.node)
            self.assertEqual((code, out.get("refused")), (1, "ledger-unwritable"), (ledger, out))
            self.assertEqual(len(self.node.published), before, "nothing is signed or published")
            self.assertIn(ledger, out["message"])
            self.assertIn("do NOT", out["message"])
        self.assertFalse(os.path.lexists(os.path.join(base, "no-such-dir")), "nothing is created")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anywhere")
    def test_sent_ledger_in_a_read_only_directory_is_refused_before_signing(self):
        locked = os.path.join(os.path.dirname(self.key_file), "locked")
        os.mkdir(locked)
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1",
                         "--ledger", os.path.join(locked, "sent.json")],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("refused")), (1, "ledger-unwritable"), out)
        self.assertEqual(len(self.node.published), before)

    def test_missing_sent_ledger_in_an_existing_directory_still_pays(self):
        ledger = os.path.join(os.path.dirname(self.key_file), "fresh-sent.json")
        before = len(self.node.published)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1",
                         "--ledger", ledger], Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid")), (0, True), out)
        self.assertEqual(len(self.node.published), before + 1)
        self.assertTrue(os.path.isfile(ledger))

    def _pay_with_ledger(self, ledger):
        return run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1",
                    "--ledger", ledger], Seller(challenge(entry())), self.node)

    def test_ledger_path_through_a_missing_directory_and_dotdot_is_refused(self):
        base = os.path.dirname(self.key_file)
        ledger = os.path.join(base, "missing", "..", "l.json")
        before = len(self.node.published)
        code, out = self._pay_with_ledger(ledger)
        self.assertEqual((code, out.get("refused")), (1, "ledger-unwritable"), out)
        self.assertEqual(len(self.node.published), before, "nothing is signed or published")
        self.assertIn(ledger, out["message"])
        # A retry is refused the same way: it never pays a second time.
        code, out = self._pay_with_ledger(ledger)
        self.assertEqual((code, out.get("refused")), (1, "ledger-unwritable"), out)
        self.assertEqual(len(self.node.published), before)
        self.assertFalse(os.path.lexists(os.path.join(base, "l.json")))

    def test_ledger_path_ending_in_a_separator_is_refused(self):
        base = os.path.dirname(self.key_file)
        for ledger in (os.path.join(base, "nope") + os.sep, base + os.sep):
            before = len(self.node.published)
            code, out = self._pay_with_ledger(ledger)
            self.assertEqual((code, out.get("refused")), (1, "ledger-unwritable"), (ledger, out))
            self.assertEqual(len(self.node.published), before, ledger)
            self.assertIn(ledger, out["message"])
        self.assertFalse(os.path.lexists(os.path.join(base, "nope")))

    def test_ledger_save_failure_after_publishing_says_paid_and_prints_the_record(self):
        ledger = self.key_file + ".pay402-sent.json"
        before = len(self.node.published)
        with mock.patch.object(pay402.os, "replace", side_effect=OSError(28, "No space left on device")):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("recorded"), out.get("error")),
                         (4, True, False, "ledger-unsaved"), out)
        self.assertNotIn("refused", out)
        self.assertEqual(len(self.node.published), before + 1, "the payment was published")
        block_hash = _hash(self.node.published[-1])
        self.assertEqual(out.get("block_hash"), block_hash, out)
        self.assertIn(block_hash, out["message"])
        self.assertIn("WAS published", out["message"])
        self.assertIn("do NOT retry", out["message"])
        self.assertIn(ledger, out["message"])
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        record = out["unsaved_record"]
        self.assertEqual(list(record), [key])
        self.assertEqual(record[key]["block_hash"], block_hash)
        self.assertFalse(os.path.exists(ledger))
        self.assertEqual([n for n in os.listdir(os.path.dirname(ledger)) if n.endswith(".tmp")], [])

    def _publish_then_raise(self, error):
        real = self.node.process

        def process(block):
            real(block)
            raise error
        return mock.patch.object(self.node, "process", side_effect=process)

    def test_lost_reply_and_ledger_save_failure_says_paid_unknown_exit_4(self):
        before = len(self.node.published)
        with self._publish_then_raise(fakenode.nanonode.NodeError("timeout", "no reply")), \
                mock.patch.object(pay402.os, "replace", side_effect=OSError(28, "No space")):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("error")), (4, "unknown", "ledger-unsaved"), out)
        self.assertEqual(len(self.node.published), before + 1)
        block_hash = _hash(self.node.published[-1])
        self.assertEqual(out.get("block_hash"), block_hash, out)
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        self.assertEqual(out["unsaved_record"][key]["block_hash"], block_hash)
        self.assertIn("MAY have been published", out["message"])

    def test_unexpected_error_after_publishing_saves_the_record_and_says_paid_unknown(self):
        ledger = self.key_file + ".pay402-sent.json"
        before = len(self.node.published)
        with self._publish_then_raise(RuntimeError("client bug")):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("recorded"), out.get("error")),
                         (5, "unknown", True, "RuntimeError"), out)
        self.assertIn("RuntimeError: client bug", out["message"])
        self.assertEqual(len(self.node.published), before + 1)
        block_hash = _hash(self.node.published[-1])
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        with open(ledger) as handle:
            saved = json.load(handle)
        self.assertEqual(saved[key]["block_hash"], block_hash, "the attempt is on disk")
        # A retry settles the same block: it never publishes a second payment.
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual(len(self.node.published) - before, 1, out)

    def test_unexpected_error_and_ledger_save_failure_says_paid_unknown_exit_4(self):
        with self._publish_then_raise(RuntimeError("client bug")), \
                mock.patch.object(pay402.os, "replace", side_effect=OSError(28, "No space")):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("error")), (4, "unknown", "ledger-unsaved"), out)
        self.assertIn("RuntimeError: client bug", out["message"])

    def test_ctrl_c_after_publishing_saves_the_record_and_reraises(self):
        ledger = self.key_file + ".pay402-sent.json"
        before = len(self.node.published)
        with self._publish_then_raise(KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                    Seller(challenge(entry())), self.node)
        self.assertEqual(len(self.node.published), before + 1)
        block_hash = _hash(self.node.published[-1])
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        self.assertTrue(os.path.exists(ledger), "Ctrl-C left the signed block unrecorded")
        with open(ledger) as handle:
            self.assertEqual(json.load(handle)[key]["block_hash"], block_hash)
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual(len(self.node.published) - before, 1, "a retry paid twice: %s" % out)

    def test_system_exit_after_publishing_saves_the_record_and_reraises(self):
        ledger = self.key_file + ".pay402-sent.json"
        with self._publish_then_raise(SystemExit(9)):
            with self.assertRaises(SystemExit):
                run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                    Seller(challenge(entry())), self.node)
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        with open(ledger) as handle:
            self.assertIn(key, json.load(handle))

    def test_ctrl_c_and_ledger_save_failure_says_paid_unknown_exit_4(self):
        with self._publish_then_raise(KeyboardInterrupt()), \
                mock.patch.object(pay402.os, "replace", side_effect=OSError(28, "No space")):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("error")), (4, "unknown", "ledger-unsaved"), out)
        self.assertIn("KeyboardInterrupt", out["message"])
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        self.assertEqual(out["unsaved_record"][key]["block_hash"], _hash(self.node.published[-1]))

    def test_replay_of_a_settled_payment_does_not_save_or_claim_unrecorded(self):
        argv = [URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"]
        code, first = run(argv, Seller(challenge(entry())), self.node)
        self.assertEqual(code, 0, first)
        before = len(self.node.published)
        with mock.patch.object(pay402.os, "replace", side_effect=OSError(28, "No space")) as rep:
            code, out = run(argv, Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("replayed_send")), (0, True, True), out)
        self.assertNotIn("ledger-unsaved", json.dumps(out))
        self.assertEqual(rep.call_count, 0, "nothing new to record, so nothing is written")
        self.assertEqual(len(self.node.published), before)

    def test_refusal_before_signing_with_an_old_record_is_a_refusal_not_paid_unknown(self):
        ledger = self.key_file + ".pay402-sent.json"
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        # A valid-shaped old record under this key whose terms differ: payments.send refuses
        # it (idempotency_conflict) before signing anything.
        content = json.dumps({key: {"to": SELLER, "amount_raw": PRICE + 1, "result": None}})
        with open(ledger, "w") as handle:
            handle.write(content)
        before = len(self.node.published)
        with mock.patch.object(pay402.os, "replace", side_effect=OSError(28, "No space")):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("refused")),
                         (1, False, "idempotency_conflict"), out)
        self.assertEqual(len(self.node.published), before)
        with open(ledger) as handle:
            self.assertEqual(handle.read(), content)

    def test_paid_unrecorded_is_not_a_refusal(self):
        self.assertFalse(issubclass(pay402.PaidUnrecorded, pay402.Refused))
        exc = pay402.PaidUnrecorded(True, "m", {"block_hash": "AB"})
        self.assertEqual((exc.reason, exc.message, exc.extra, exc.paid),
                         ("ledger-unsaved", "m", {"block_hash": "AB"}, True))

    def test_readme_documents_interrupts_second_save_failure_and_the_class(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = " ".join(handle.read().split())
        self.assertIn("Ctrl-C (`KeyboardInterrupt`)", readme)
        self.assertIn("If that save fails as well, the error is NOT raised: pay402 exits 4", readme)
        self.assertIn("deliberately not a subclass of `pay402.Refused`", readme)
        self.assertIn("NOT\na Refused subclass", pay402.__doc__)

    def _sigint_inside_fsync(self):
        real = os.fsync

        def fsync(fd):
            os.kill(os.getpid(), signal.SIGINT)   # a real Ctrl-C arriving mid-save
            return real(fd)
        return mock.patch.object(pay402.os, "fsync", side_effect=fsync)

    def _settles_without_a_second_payment(self, before):
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        Seller(challenge(entry())), self.node)
        self.assertEqual(len(self.node.published) - before, 1, "a retry paid twice: %s" % out)
        return code, out

    def test_ctrl_c_during_the_success_save_is_held_until_the_record_is_on_disk(self):
        ledger = self.key_file + ".pay402-sent.json"
        before = len(self.node.published)
        with self._sigint_inside_fsync():
            with self.assertRaises(KeyboardInterrupt):
                run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                    Seller(challenge(entry())), self.node)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        self.assertTrue(os.path.exists(ledger), "Ctrl-C mid-save left the block unrecorded")
        with open(ledger) as handle:
            self.assertEqual(json.load(handle)[key]["block_hash"], _hash(self.node.published[-1]))
        self._settles_without_a_second_payment(before)

    def test_second_ctrl_c_during_the_rescue_save_still_records_the_block(self):
        ledger = self.key_file + ".pay402-sent.json"
        before = len(self.node.published)
        with self._publish_then_raise(KeyboardInterrupt()), self._sigint_inside_fsync():
            with self.assertRaises(KeyboardInterrupt):
                run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                    Seller(challenge(entry())), self.node)
        self.assertTrue(os.path.exists(ledger), "the second Ctrl-C left the block unrecorded")
        self._settles_without_a_second_payment(before)

    def test_keyboard_interrupt_raised_inside_the_save_reports_unrecorded_exit_4(self):
        with mock.patch.object(pay402.os, "fsync", side_effect=KeyboardInterrupt()):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("recorded"), out.get("error")),
                         (4, True, False, "ledger-unsaved"), out)
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        self.assertEqual(out["unsaved_record"][key]["block_hash"], _hash(self.node.published[-1]))

    def test_timeout_after_handing_over_the_block_says_paid_unknown_exit_5(self):
        ledger = self.key_file + ".pay402-sent.json"
        before = len(self.node.published)
        with self._publish_then_raise(fakenode.nanonode.NodeError("timeout", "no reply")):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("recorded"), out.get("error")),
                         (5, "unknown", True, "timeout"), out)
        self.assertNotIn("refused", out)
        block_hash = _hash(self.node.published[-1])
        self.assertEqual(out.get("block_hash"), block_hash)
        self.assertIn("retry with the same ledger is safe", out["message"])
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        with open(ledger) as handle:
            self.assertEqual(json.load(handle)[key]["block_hash"], block_hash)
        code, out = self._settles_without_a_second_payment(before)
        self.assertEqual((code, out.get("paid")), (0, True), out)

    def test_seller_unreachable_after_paying_is_paid_exit_3_not_refused(self):
        def seller(url, method="GET", data=None, headers=None, timeout=20):
            if "X-PAYMENT" in (headers or {}):
                raise pay402.Refused("unreachable", "could not reach %s" % url)
            return 402, {}, json.dumps(challenge(entry())).encode()
        code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                        seller, self.node)
        self.assertEqual((code, out.get("paid"), out.get("seller_status"), out.get("seller_error")),
                         (3, True, None, "unreachable"), out)
        self.assertNotIn("refused", out)

    def test_readme_documents_exit_5(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = " ".join(handle.read().split())
        self.assertIn("exits 5 with `\"paid\": \"unknown\"`, `\"recorded\": true`", readme)
        self.assertIn("a retry with the same ledger is safe", readme)
        self.assertIn("Ctrl-C is held until the ledger is written", readme)

    def test_retry_that_still_cannot_settle_says_paid_unknown_exit_5_not_paid_false(self):
        ledger = self.key_file + ".pay402-sent.json"
        argv = [URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"]
        before = len(self.node.published)
        with self._publish_then_raise(fakenode.nanonode.NodeError("timeout", "no reply")):
            code, out = run(argv, Seller(challenge(entry())), self.node)
        self.assertEqual(code, 5, out)
        block_hash = _hash(self.node.published[-1])
        with open(ledger) as handle:
            content = handle.read()
        # The retry cannot reach the node either: settling fails (send_outcome_unknown) and
        # the ledger is unchanged. The first block may have landed: never "paid": false.
        with mock.patch.object(self.node, "account_info",
                               side_effect=fakenode.nanonode.NodeError("timeout", "no reply")):
            code, out = run(argv, Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("recorded"), out.get("error")),
                         (5, "unknown", True, "send_outcome_unknown"), out)
        self.assertNotIn("refused", out)
        self.assertEqual(out.get("block_hash"), block_hash, out)
        with open(ledger) as handle:
            self.assertEqual(handle.read(), content)
        self.assertEqual(len(self.node.published) - before, 1)
        with mock.patch.object(self.node, "account_info",
                               side_effect=fakenode.nanonode.NodeError("timeout", "no reply")):
            with self.assertRaises(pay402.PaidUnknown) as caught:
                pay402.pay(URL, entry(), pay402.plan(URL, [entry()], "0.1")[0], self.key_file,
                           "0.1", self.node, ledger=ledger, sleep=lambda s: None)
        self.assertEqual(caught.exception.paid, "unknown")
        self._settles_without_a_second_payment(before)

    def test_ctrl_c_between_the_send_raising_and_the_rescue_save_still_records_the_block(self):
        ledger = self.key_file + ".pay402-sent.json"
        before = len(self.node.published)
        real = pay402._ledger_save_after_send

        def save(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGINT)   # lands before the save's own guard starts
            return real(*args, **kwargs)
        with self._publish_then_raise(fakenode.nanonode.NodeError("timeout", "no reply")), \
                mock.patch.object(pay402, "_ledger_save_after_send", side_effect=save):
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("recorded")), (5, "unknown", True), out)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        self.assertTrue(os.path.exists(ledger), "Ctrl-C before the rescue save left it unsaved")
        with open(ledger) as handle:
            self.assertEqual(json.load(handle)[key]["block_hash"], _hash(self.node.published[-1]))
        self._settles_without_a_second_payment(before)

    def test_held_ctrl_c_under_sig_dfl_is_redelivered_and_ends_the_process(self):
        import subprocess
        script = ("import os, signal, sys\n"
                  "sys.path.insert(0, %r)\n"
                  "import pay402\n"
                  "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
                  "with pay402._SigintDeferred():\n"
                  "    os.kill(os.getpid(), signal.SIGINT)\n"
                  "    print('held', flush=True)\n"
                  "print('continued past a held Ctrl-C', flush=True)\n" % ROOT)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.stdout, "held\n", proc.stderr)
        self.assertEqual(proc.returncode, -signal.SIGINT, proc.stderr)

    def test_a_none_previous_sigint_handler_is_restored_as_the_default(self):
        real = signal.signal
        calls = []

        def fake(signum, handler):
            previous = real(signum, handler)
            calls.append(handler)
            return None if len(calls) == 1 else previous   # "not installed from Python"
        real(signal.SIGINT, signal.default_int_handler)
        try:
            with mock.patch.object(pay402.signal, "signal", side_effect=fake):
                with self.assertRaises(KeyboardInterrupt):
                    with pay402._SigintDeferred():
                        os.kill(os.getpid(), signal.SIGINT)
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        finally:
            real(signal.SIGINT, signal.default_int_handler)

    def test_readme_names_paid_unknown_for_library_callers(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = " ".join(handle.read().split())
        self.assertIn("For exit 5 it raises `pay402.PaidUnknown`", readme)
        self.assertIn("A retry that reaches the send and still cannot settle the block", readme)
        self.assertNotIn("exits 5 again, never 1", readme)
        self.assertIn("a retry that reaches the send", " ".join(pay402.__doc__.split()))
        self.assertIn("It raises PaidUnknown for exit 5", " ".join(pay402.__doc__.split()))

    def test_unexpected_error_before_signing_is_raised_and_writes_nothing(self):
        ledger = self.key_file + ".pay402-sent.json"
        with mock.patch.object(self.node, "account_info", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                    Seller(challenge(entry())), self.node)
        self.assertFalse(os.path.exists(ledger))

    def test_wrong_shape_entry_under_this_payments_key_is_refused(self):
        ledger = self.key_file + ".pay402-sent.json"
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        shapes = [
            {},
            {"to": SELLER},
            {"amount_raw": PRICE},
            {"to": SELLER, "amount_raw": str(PRICE)},
            {"to": SELLER, "amount_raw": True, "result": None},
            {"to": 5, "amount_raw": PRICE, "result": None},
            {"to": SELLER, "amount_raw": PRICE, "result": "bad"},
            {"to": SELLER, "amount_raw": PRICE, "result": {}},
            {"to": SELLER, "amount_raw": PRICE, "result": {"block_hash": 7}},
            {"to": SELLER, "amount_raw": PRICE, "result": None, "block_hash": 5},
            {"to": SELLER, "amount_raw": PRICE, "result": None, "block_hash": "AB",
             "previous": 3},
            {"to": SELLER, "amount_raw": PRICE, "result": None, "block_hash": "AB",
             "previous": "CD", "block": "x"},
        ]
        for shape in shapes:
            content = json.dumps({key: shape})
            with open(ledger, "w") as handle:
                handle.write(content)
            before = len(self.node.published)
            code, out = run([URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"],
                            Seller(challenge(entry())), self.node)
            self.assertEqual((code, out.get("refused")), (1, "ledger-malformed"), (shape, out))
            self.assertEqual(len(self.node.published), before, shape)
            self.assertIn(ledger, out["message"], shape)
            self.assertIn("do NOT delete it", out["message"], shape)
            with open(ledger) as handle:
                self.assertEqual(handle.read(), content, shape)


class LedgerLockAndFinalRefusals(Funded):
    """Two runs on one sent ledger never both sign; final refusals say how pay402 gets out."""

    def argv(self):
        return [URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"]

    def ledger(self):
        return self.key_file + ".pay402-sent.json"

    def _pay_in_thread(self, results, name):
        summary = pay402.plan(URL, [entry()], "0.1")[0]
        try:
            results[name] = pay402.pay(URL, entry(), summary, self.key_file, "0.1", self.node,
                                       fetcher=Seller(challenge(entry())), sleep=lambda s: None)
        except BaseException as exc:            # pragma: no cover - reported by the assertions
            results[name] = exc

    def test_second_concurrent_run_waits_for_the_first_save_and_never_pays_twice(self):
        real_send = payments.send
        first_sent, release = threading.Event(), threading.Event()
        calls = []

        def send(*args, **kwargs):
            calls.append(threading.current_thread().name)
            result = real_send(*args, **kwargs)
            if len(calls) == 1:
                # Run A has published its block but not yet saved the ledger.
                first_sent.set()
                release.wait(10)
            return result
        before = len(self.node.published)
        results = {}
        with mock.patch.object(pay402.payments, "send", side_effect=send):
            a = threading.Thread(target=self._pay_in_thread, args=(results, "A"), name="A")
            a.start()
            self.assertTrue(first_sent.wait(10))
            b = threading.Thread(target=self._pay_in_thread, args=(results, "B"), name="B")
            b.start()
            b.join(1.0)
            # While A has not saved, B must not have loaded the ledger or reached the send.
            alive, seen = b.is_alive(), list(calls)
            release.set()
            a.join(10)
            b.join(10)
        self.assertTrue(alive, "B finished while A had not saved: %r" % results.get("B"))
        self.assertEqual(seen, ["A"])
        self.assertIsInstance(results.get("A"), dict, results)
        self.assertIsInstance(results.get("B"), dict, results)
        self.assertEqual(len(self.node.published) - before, 1, "two runs on one ledger paid twice")
        self.assertEqual(results["A"]["block_hash"], results["B"]["block_hash"], results)
        self.assertTrue(results["B"]["replayed_send"], results)
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        with open(self.ledger()) as handle:
            self.assertEqual(json.load(handle)[key]["result"]["block_hash"],
                             results["A"]["block_hash"])

    def test_a_run_does_not_load_the_ledger_while_its_lock_is_held(self):
        done = threading.Event()
        results = {}
        before = len(self.node.published)

        def worker():
            self._pay_in_thread(results, "B")
            done.set()
        with pay402._file_lock(self.ledger()):
            t = threading.Thread(target=worker)
            t.start()
            waited = not done.wait(1.0)
            published_while_locked = len(self.node.published) - before
        t.join(10)
        self.assertTrue(waited, "a run went ahead while the ledger was locked: %r" % results)
        self.assertEqual(published_while_locked, 0)
        self.assertTrue(done.is_set())
        self.assertIs(results["B"]["paid"], True, results)

    def _unsettled_record(self, block_hash):
        return {"to": SELLER, "amount_raw": PRICE, "result": None, "block_hash": block_hash,
                "previous": "CD" * 32, "block": {"type": "state"}}

    def test_duplicate_send_with_this_keys_result_null_is_final_exit_1(self):
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        other = pay402.idempotency_key(URL + "?b", SELLER, PRICE)
        block_hash = "AB" * 32
        content = json.dumps({
            key: self._unsettled_record(block_hash),
            other: {"to": SELLER, "amount_raw": PRICE, "block_hash": block_hash,
                    "result": {"block_hash": block_hash, "amount_xno": "0.05", "to": SELLER,
                               "confirmed": True}}})
        with open(self.ledger(), "w") as handle:
            handle.write(content)
        before = len(self.node.published)
        for _ in range(2):
            code, out = run(self.argv(), Seller(challenge(entry())), self.node)
            self.assertEqual((code, out.get("paid"), out.get("refused")),
                             (1, False, "duplicate_send"), out)
            self.assertNotIn("Retry", out["message"])
            self.assertIn("remove that one entry", out["message"])
            self.assertIn(key, out["message"])
            self.assertIn(block_hash, out["message"])
        self.assertEqual(len(self.node.published), before)
        with open(self.ledger()) as handle:
            self.assertEqual(handle.read(), content)

    def test_send_did_not_land_names_what_pay402_can_do_not_a_new_key(self):
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        block_hash = "EF" * 32
        with open(self.ledger(), "w") as handle:
            json.dump({key: self._unsettled_record(block_hash)}, handle)
        error = payments.ToolError("send_did_not_land", "block %s can never land. To pay, retry "
                                   "with a NEW idempotency_key." % block_hash, 409)
        with mock.patch.object(pay402.payments, "send", side_effect=error):
            code, out = run(self.argv(), Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("refused")),
                         (1, False, "send_did_not_land"), out)
        self.assertNotIn("NEW idempotency", out["message"])
        self.assertIn("cannot switch to a new one", out["message"])
        self.assertIn("remove that one entry", out["message"])
        self.assertIn(key, out["message"])
        self.assertIn(self.ledger(), out["message"])

    def test_readme_says_ctrl_c_is_held_for_the_whole_send_and_the_lock(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = " ".join(handle.read().split())
        self.assertIn("it is held for the whole send, not only the save", readme)
        self.assertIn("exclusive `flock` on `LEDGER.lock`", readme)
        self.assertIn("Ctrl-C is held for the whole send", " ".join(pay402.__doc__.split()))


class LedgerLockFollowsTheRealFile(Funded):
    """The ledger lock and save act on the file a symlinked ledger points at, the lock not
    opening is a JSON refusal, and duplicate_send advice holds whether the holder settled."""

    def argv(self):
        return [URL, "--pay", "--key-file", self.key_file, "--max-xno", "0.1"]

    def real_ledger(self):
        return self.key_file + ".pay402-sent.json"

    def link_ledger(self):
        return os.path.join(self.tmp.name, "via-link.json")

    def _pay_in_thread(self, results, name, ledger):
        summary = pay402.plan(URL, [entry()], "0.1")[0]
        try:
            results[name] = pay402.pay(URL, entry(), summary, self.key_file, "0.1", self.node,
                                       ledger=ledger, fetcher=Seller(challenge(entry())),
                                       sleep=lambda s: None)
        except BaseException as exc:            # pragma: no cover - reported by the assertions
            results[name] = exc

    @unittest.skipUnless(hasattr(os, "symlink") and pay402.fcntl is not None,
                         "needs symlinks and fcntl")
    def test_symlink_and_real_path_share_one_lock_and_never_pay_twice(self):
        with open(self.real_ledger(), "w") as handle:
            handle.write("{}")
        os.symlink(self.real_ledger(), self.link_ledger())
        real_send = payments.send
        first_sent, release = threading.Event(), threading.Event()
        calls = []

        def send(*args, **kwargs):
            calls.append(threading.current_thread().name)
            result = real_send(*args, **kwargs)
            if len(calls) == 1:
                first_sent.set()
                release.wait(10)
            return result
        before = len(self.node.published)
        results = {}
        with mock.patch.object(pay402.payments, "send", side_effect=send):
            a = threading.Thread(target=self._pay_in_thread,
                                 args=(results, "A", self.real_ledger()), name="A")
            a.start()
            self.assertTrue(first_sent.wait(10))
            b = threading.Thread(target=self._pay_in_thread,
                                 args=(results, "B", self.link_ledger()), name="B")
            b.start()
            b.join(1.0)
            seen = list(calls)
            release.set()
            a.join(10)
            b.join(10)
        self.assertEqual(seen, ["A"], "the run through the link did not wait for the lock")
        self.assertEqual(len(self.node.published) - before, 1,
                         "a symlinked ledger and its real path paid twice: %r" % results)
        self.assertEqual(results["A"]["block_hash"], results["B"]["block_hash"], results)
        self.assertTrue(results["B"]["replayed_send"], results)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_saving_through_a_symlinked_ledger_writes_the_real_file_and_keeps_the_link(self):
        with open(self.real_ledger(), "w") as handle:
            handle.write("{}")
        os.symlink(self.real_ledger(), self.link_ledger())
        code, out = run(self.argv() + ["--ledger", self.link_ledger()],
                        Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid")), (0, True), out)
        self.assertTrue(os.path.islink(self.link_ledger()), "the link was replaced by a file")
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        with open(self.real_ledger()) as handle:
            self.assertEqual(json.load(handle)[key]["result"]["block_hash"], out["block_hash"])
        # A second run through the real path sees the record and does not pay again.
        before = len(self.node.published)
        code, out2 = run(self.argv(), Seller(challenge(entry())), self.node)
        self.assertEqual((code, out2["block_hash"]), (0, out["block_hash"]), out2)
        self.assertEqual(len(self.node.published), before)

    @unittest.skipUnless(pay402.fcntl is not None, "needs fcntl")
    def test_flock_failure_is_a_json_refusal_before_signing(self):
        import errno
        before = len(self.node.published)
        failing = OSError(errno.ENOLCK, "No locks available")
        with mock.patch.object(pay402.fcntl, "flock", side_effect=failing):
            code, out = run(self.argv(), Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("refused")),
                         (1, False, "ledger-unwritable"), out)
        self.assertIn("No locks available", out["message"])
        self.assertEqual(len(self.node.published), before, "nothing is signed or published")
        self.assertFalse(os.path.exists(self.real_ledger()))

    def _block_this_run_would_sign(self):
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        # Sign this URL's block against a throwaway copy of the ledger and node state: the
        # block hash depends only on account, frontier, destination and amount.
        import copy
        clone = copy.deepcopy(self.node)
        sent = {}
        env = {"NANO_WALLET_ALLOW_SEND": "1", "NANO_WALLET_MAX_SEND_XNO": "0.1"}
        source = keystore.KeyStore()
        address = source.load_file(self.key_file)
        result = payments.send(address, SELLER, "0.05", key, clone, source, sent, env=env)
        return result["block_hash"]

    def test_duplicate_send_held_by_an_unsettled_entry_says_retry_that_one_first(self):
        block_hash = self._block_this_run_would_sign()
        key = pay402.idempotency_key(URL, SELLER, PRICE)
        other = pay402.idempotency_key(URL + "?b", SELLER, PRICE)
        content = json.dumps({other: {"to": SELLER, "amount_raw": PRICE, "result": None,
                                      "block_hash": block_hash, "previous": "CD" * 32,
                                      "block": {"type": "state"}}})
        with open(self.real_ledger(), "w") as handle:
            handle.write(content)
        before = len(self.node.published)
        code, out = run(self.argv(), Seller(challenge(entry())), self.node)
        self.assertEqual((code, out.get("paid"), out.get("refused")),
                         (1, False, "duplicate_send"), out)
        message = out["message"]
        self.assertNotIn("already settled", message)
        self.assertNotIn("remove that one entry", message)
        self.assertIn("not settled", message)
        self.assertIn(other, message)
        self.assertIn("first", message)
        self.assertEqual(len(self.node.published), before)
        with open(self.real_ledger()) as handle:
            self.assertEqual(handle.read(), content)

    def test_readme_upgrading_lists_the_new_exit_codes_and_exceptions(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = " ".join(handle.read().split())
        for text in ("### Upgrading: new exit codes and exceptions for --pay",
                     "This used to be exit 1 with `\"paid\": false`",
                     "Compare `out[\"paid\"] == True`",
                     "except (Refused, PaidUnknown, PaidUnrecorded)",
                     "`.lock` files are left next to the sent ledger"):
            self.assertIn(text, readme)


def _hash(block):
    return fakenode._hash_of(block)


if __name__ == "__main__":
    unittest.main()
