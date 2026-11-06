"""nano-pay402: dry-run by default, refuses before it signs, never pays twice.

No test here opens a socket except the redirect law (127.0.0.1 only); the seller is a
function, the node is fakenode.FakeNode, and no real payment is possible.
"""
import base64
import contextlib
import io
import json
import os
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


def _hash(block):
    return fakenode._hash_of(block)


if __name__ == "__main__":
    unittest.main()
