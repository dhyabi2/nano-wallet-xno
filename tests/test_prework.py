"""Work found before the payment, which is the only way a send gets work with no node.

A send needs 64x a receive's proof-of-work and no free public node answers
`work_generate`, so `work.solve` refuses the send threshold inline - minutes of
CPU between "pay this" and the block going out is not a wallet. The way out is
not a faster hash: it is doing the work EARLIER. Work is computed over the
account's frontier and nothing else, and the frontier is fixed the moment the
account's last block confirms, so the next send's work can be found while
nothing waits on it.

The vector below is the evidence that the hard threshold is reachable here.
`SEND_WORK` was found by `work.solve_parallel` on four cores in 137 seconds
(9m01s of CPU) for the same real mainnet root `tests/test_work.py` pins, and it
clears `SEND_THRESHOLD`. Pinning it means this suite proves a send's work is
findable without spending those two minutes again - and because the root is one
the live network already accepted a (receive-grade) block on, the byte order it
is read under is the network's, not this repository's.

Nothing here opens a socket: `urlopen` is replaced for the tests that drive
`HttpNanoNode`, exactly as `tests/test_http_node.py` does it.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keystore as _keystore  # noqa: E402
import nanonode  # noqa: E402
import payments  # noqa: E402
import prework  # noqa: E402
import work  # noqa: E402

# The real open block's root, as tests/test_work.py reads it off the mainnet.
REAL_ROOT_HEX = "8789CF4B88407CB006F5A53F6FEEECFCDC6E56F8AE51B9BBF806317A33643490"
REAL_ROOT = bytes.fromhex(REAL_ROOT_HEX)

# Receive-grade work for that root: the one the live network accepted.
RECEIVE_WORK = "58fc8abfa35fcfee"

# Send-grade work for the same root, found here by `solve_parallel`.
SEND_WORK = "0ba27e49039735a0"

EASY = 0x8000000000000000          # one hash in two clears it
OTHER_ROOT = "A" * 64              # a root the vectors above are NOT work for
ONE_XNO = 10 ** 30


class TheSendThresholdIsReachableAheadOfTime(unittest.TestCase):
    """The claim the whole module exists to make."""

    def test_the_pinned_vector_clears_the_send_threshold(self):
        self.assertTrue(work.validates(REAL_ROOT, SEND_WORK, work.SEND_THRESHOLD),
                        "the send-grade vector no longer validates - if the validator "
                        "changed, every block this wallet ever signed is suspect")

    def test_the_networks_own_receive_work_does_not(self):
        # Both vectors are for the same root, so this is the two thresholds
        # being different and not the two roots being different.
        self.assertTrue(work.validates(REAL_ROOT, RECEIVE_WORK, work.RECEIVE_THRESHOLD))
        self.assertFalse(work.validates(REAL_ROOT, RECEIVE_WORK, work.SEND_THRESHOLD))

    def test_send_grade_work_is_also_valid_for_a_receive(self):
        # Why one cache entry per root serves every subtype: the send
        # threshold is the higher number, so clearing it clears the other.
        self.assertTrue(work.validates(REAL_ROOT, SEND_WORK, work.RECEIVE_THRESHOLD))

    def test_the_inline_path_still_refuses_the_send_threshold(self):
        # The split this change rests on: ahead of time, yes; inside a
        # payment, never.
        with self.assertRaises(work.WorkUnavailable):
            work.solve(REAL_ROOT, work.SEND_THRESHOLD)


class WhatASearchCosts(unittest.TestCase):
    def test_expected_attempts_at_the_two_thresholds(self):
        # 2**64 / (2**64 - threshold), which is 2**23 and 2**29 exactly.
        self.assertEqual(work.expected_attempts(work.RECEIVE_THRESHOLD), 2 ** 23)
        self.assertEqual(work.expected_attempts(work.SEND_THRESHOLD), 2 ** 29)

    def test_a_send_is_sixty_four_times_a_receive(self):
        self.assertEqual(
            work.expected_attempts(work.SEND_THRESHOLD)
            / work.expected_attempts(work.RECEIVE_THRESHOLD), 64)

    def test_a_threshold_of_zero_succeeds_on_the_first_hash(self):
        self.assertEqual(work.expected_attempts(0), 1.0)

    def test_a_threshold_outside_sixty_four_bits_is_refused(self):
        for bad in (-1, 1 << 64, 1 << 70):
            with self.assertRaises(ValueError):
                work.expected_attempts(bad)

    def test_the_measured_rate_is_a_positive_number_of_hashes(self):
        self.assertGreater(work.measure_rate(0.05), 0)

    def test_a_rate_cannot_be_measured_over_no_time(self):
        for bad in (0, -1.0):
            with self.assertRaises(ValueError):
                work.measure_rate(bad)

    def test_an_estimate_is_the_attempts_divided_by_the_rate_and_the_cores(self):
        one = work.estimate_seconds(work.SEND_THRESHOLD, rate=1000.0, workers=1)
        self.assertAlmostEqual(one, 2 ** 29 / 1000.0)
        four = work.estimate_seconds(work.SEND_THRESHOLD, rate=1000.0, workers=4)
        self.assertAlmostEqual(four, one / 4)

    def test_an_estimate_refuses_an_impossible_rate(self):
        for bad in (0.0, -5.0):
            with self.assertRaises(ValueError):
                work.estimate_seconds(work.SEND_THRESHOLD, rate=bad)

    def test_worker_count_defaults_to_at_least_one_core(self):
        self.assertGreaterEqual(work.worker_count(), 1)
        self.assertEqual(work.worker_count(3), 3)

    def test_fewer_than_one_worker_is_refused(self):
        for bad in (0, -2):
            with self.assertRaises(ValueError):
                work.worker_count(bad)


class SearchingOnEveryCore(unittest.TestCase):
    def test_what_it_finds_validates_for_the_root_it_was_asked_about(self):
        found = work.solve_parallel(REAL_ROOT, EASY, budget_seconds=30.0, workers=2)
        self.assertTrue(work.validates(REAL_ROOT, found, EASY))
        self.assertEqual(len(found), 16)

    def test_it_finds_a_receive_for_a_root_it_has_never_seen(self):
        root = bytes(range(32))
        found = work.solve_parallel(root, work.RECEIVE_THRESHOLD, budget_seconds=120.0)
        self.assertTrue(work.validates(root, found, work.RECEIVE_THRESHOLD))

    def test_one_worker_searches_in_this_process_and_still_finds_it(self):
        # The single-core and no-fork path. Same search, no child processes.
        found = work.solve_parallel(REAL_ROOT, EASY, budget_seconds=30.0, workers=1)
        self.assertTrue(work.validates(REAL_ROOT, found, EASY))

    def test_no_fork_falls_back_to_this_process_rather_than_spawning(self):
        # A spawning child re-imports __main__, which this package runs at
        # import. The fallback is what keeps that from ever being reached.
        with mock.patch.object(work, "_fork_context", lambda: None):
            found = work.solve_parallel(REAL_ROOT, EASY, budget_seconds=30.0, workers=4)
        self.assertTrue(work.validates(REAL_ROOT, found, EASY))

    def test_an_exhausted_budget_is_reported_not_waited_out(self):
        with self.assertRaises(work.WorkUnavailable) as caught:
            work.solve_parallel(REAL_ROOT, work.SEND_THRESHOLD,
                                budget_seconds=0.0, workers=2)
        self.assertIn("no proof-of-work found", str(caught.exception))

    def test_a_root_of_the_wrong_length_is_named(self):
        with self.assertRaises(ValueError):
            work.solve_parallel(b"short", EASY)

    def test_a_threshold_out_of_range_is_refused_before_any_worker_starts(self):
        with self.assertRaises(ValueError):
            work.solve_parallel(REAL_ROOT, 1 << 64)

    def test_work_a_worker_got_wrong_is_refused_rather_than_returned(self):
        # Defence in depth: the parent re-derives the difficulty itself, so a
        # worker bug cannot put an under-worked block on the network.
        class _Liar(object):
            def Event(self):
                return mock.Mock(**{"is_set.return_value": True, "set.return_value": None})

            def Queue(self):
                answers = mock.Mock()
                answers.get.return_value = "ffffffffffffffff"   # not work for this root
                return answers

            def Process(self, *a, **k):
                return mock.Mock(**{"start.return_value": None,
                                    "join.return_value": None,
                                    "is_alive.return_value": False})

        with self.assertRaises(work.WorkUnavailable) as caught:
            work.solve_parallel(REAL_ROOT, work.SEND_THRESHOLD, workers=2, _context=_Liar())
        self.assertIn("does not validate", str(caught.exception))


class _CacheCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="prework-test-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.cache = prework.WorkCache(self.directory)


class WhatTheCacheWillAnswerWith(_CacheCase):
    def test_work_put_in_comes_back_out(self):
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self.assertEqual(self.cache.get(REAL_ROOT_HEX, work.SEND_THRESHOLD), SEND_WORK)

    def test_a_root_is_read_in_either_case_and_with_surrounding_space(self):
        self.cache.put(REAL_ROOT_HEX.lower(), SEND_WORK)
        self.assertEqual(self.cache.get("  " + REAL_ROOT_HEX + "\n"), SEND_WORK)

    def test_receive_grade_work_is_not_offered_for_a_send(self):
        # The whole point. A hit here would put an under-worked send on the
        # network, which a node would reject - and a wallet that cannot tell
        # the two apart would do it on every payment.
        self.cache.put(REAL_ROOT_HEX, RECEIVE_WORK)
        self.assertEqual(self.cache.get(REAL_ROOT_HEX, work.RECEIVE_THRESHOLD), RECEIVE_WORK)
        self.assertIsNone(self.cache.get(REAL_ROOT_HEX, work.SEND_THRESHOLD))

    def test_a_missing_entry_and_a_missing_directory_are_both_misses(self):
        self.assertIsNone(self.cache.get(REAL_ROOT_HEX))
        self.assertIsNone(prework.WorkCache(self.directory + "/nope").get(REAL_ROOT_HEX))

    def test_a_malformed_root_is_a_miss_not_an_exception_on_the_read_path(self):
        for bad in ("", "zz", REAL_ROOT_HEX[:-1], 7, None):
            self.assertIsNone(self.cache.get(bad), repr(bad))

    def test_a_file_that_is_not_an_entry_is_a_miss(self):
        path = self.cache.path_for(REAL_ROOT_HEX)
        for body in ("", "not json", "[]", '"text"', "{}", '{"work": 5}',
                     '{"work": "' + SEND_WORK[:-2] + '"}'):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            self.assertIsNone(self.cache.get(REAL_ROOT_HEX), body)

    def test_work_for_another_root_cannot_answer_for_this_one(self):
        # The root is re-derived from the FILE NAME, so an entry that claims a
        # root it is not named for is worthless rather than dangerous. Written
        # with the pinned vector rather than a freshly searched nonce: against
        # an easy threshold a random nonce validates for BOTH roots about half
        # the time, so such a test would pass for a reason unrelated to what it
        # claims. `RECEIVE_WORK` is real work for `REAL_ROOT` (0xffffff63...)
        # and is 0x8a95eadd... for `OTHER_ROOT`, far below any threshold, so
        # both directions below are facts about these two numbers.
        with open(self.cache.path_for(OTHER_ROOT), "w", encoding="utf-8") as handle:
            json.dump({"root": REAL_ROOT_HEX, "work": RECEIVE_WORK}, handle)
        self.assertIsNone(self.cache.get(OTHER_ROOT, work.RECEIVE_THRESHOLD))
        # ... and it is not that the file is unreadable: named for its own root
        # the very same bytes answer.
        self.cache.put(REAL_ROOT_HEX, RECEIVE_WORK)
        self.assertEqual(self.cache.get(REAL_ROOT_HEX, work.RECEIVE_THRESHOLD),
                         RECEIVE_WORK)

    def test_has_is_get_as_a_question(self):
        self.assertFalse(self.cache.has(REAL_ROOT_HEX))
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self.assertTrue(self.cache.has(REAL_ROOT_HEX))


class WhatTheCacheRefusesToStore(_CacheCase):
    def test_something_that_is_not_work_for_this_root_is_not_stored(self):
        with self.assertRaises(prework.CacheError):
            self.cache.put(REAL_ROOT_HEX, "ffffffffffffffff")
        self.assertFalse(os.path.exists(self.cache.path_for(REAL_ROOT_HEX)))

    def test_work_below_even_the_receive_threshold_is_not_stored(self):
        easy = work.solve(REAL_ROOT, EASY)
        if work.validates(REAL_ROOT, easy, work.RECEIVE_THRESHOLD):   # 1 in 2**41
            self.skipTest("the easy nonce happened to be real work")
        with self.assertRaises(prework.CacheError) as caught:
            self.cache.put(REAL_ROOT_HEX, easy)
        self.assertIn("receive threshold", str(caught.exception))

    def test_a_malformed_root_is_named_on_the_write_path(self):
        for bad in ("", "zz", REAL_ROOT_HEX + "00", 7, None):
            with self.assertRaises(prework.CacheError):
                self.cache.put(bad, SEND_WORK)

    def test_malformed_work_is_refused_rather_than_stored(self):
        for bad in ("", "ff", "zzzzzzzzzzzzzzzz"):
            with self.assertRaises((prework.CacheError, ValueError)):
                self.cache.put(REAL_ROOT_HEX, bad)

    def test_an_entry_records_which_threshold_it_covers(self):
        entry = self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self.assertTrue(entry["covers_send"])
        self.assertEqual(entry["difficulty"],
                         "%#x" % work.difficulty(REAL_ROOT, SEND_WORK))
        self.assertFalse(self.cache.put(REAL_ROOT_HEX, RECEIVE_WORK)["covers_send"])

    def test_a_write_leaves_no_temporary_file_behind(self):
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self.cache.want("B" * 64)
        leftovers = [n for n in os.listdir(self.directory) if n.startswith(".work-")]
        self.assertEqual(leftovers, [])


class WhatWorkIsNeededNext(_CacheCase):
    def test_a_wanted_root_is_pending_until_it_has_work(self):
        self.cache.want(REAL_ROOT_HEX)
        self.assertEqual(self.cache.pending(), [REAL_ROOT_HEX])
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self.assertEqual(self.cache.pending(), [])

    def test_receive_grade_work_leaves_the_root_pending(self):
        # Pending means "cannot send yet", so work that only covers a receive
        # does not clear it.
        self.cache.want(REAL_ROOT_HEX)
        self.cache.put(REAL_ROOT_HEX, RECEIVE_WORK)
        self.assertEqual(self.cache.pending(), [REAL_ROOT_HEX])

    def test_the_newest_want_is_first_and_is_not_duplicated(self):
        self.cache.want(REAL_ROOT_HEX)
        self.cache.want("B" * 64)
        self.cache.want(REAL_ROOT_HEX)
        self.assertEqual(self.cache.pending(), [REAL_ROOT_HEX, "B" * 64])

    def test_one_account_sending_repeatedly_cannot_evict_another_accounts_root(self):
        # A wallet that publishes on one account over and over must not fill the
        # bounded list with copies of that root and push the other accounts out.
        # What actually holds that is `pending`'s dedupe on the READ: `want`
        # writes the list that comes back from it, so the file cannot grow past
        # one duplicate however many times the same root is wanted. `want`'s own
        # `if r != root` is therefore belt and braces - removing it changes no
        # reachable behaviour, which a mutation run confirmed, so nothing here
        # pretends to test it.
        self.cache.want(REAL_ROOT_HEX)
        for _ in range(prework.MAX_PENDING):
            self.cache.want(OTHER_ROOT)
        self.assertEqual(self.cache.pending(), [OTHER_ROOT, REAL_ROOT_HEX])

    def test_pending_is_bounded(self):
        for index in range(prework.MAX_PENDING + 5):
            self.cache.want("%064X" % index)
        self.assertEqual(len(self.cache.pending()), prework.MAX_PENDING)

    def test_a_want_that_cannot_be_filed_does_not_raise(self):
        # It is called just after money moved. Nothing about a hint may
        # propagate out of there.
        self.cache.want("not a root")
        self.assertEqual(self.cache.pending(), [])
        unwritable = prework.WorkCache(os.path.join(self.directory, "wall", "deeper"))
        with mock.patch.object(os, "makedirs", side_effect=OSError("no")):
            unwritable.want(REAL_ROOT_HEX)
        self.assertEqual(unwritable.pending(), [])

    def test_a_junk_pending_file_is_read_as_nothing_pending(self):
        path = os.path.join(self.directory, prework.PENDING_FILE)
        for body in ("", "null", "[]", '{"roots": 5}', '{"roots": ["nope", 7]}'):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            self.assertEqual(self.cache.pending(), [], body)

    def test_roots_lists_what_it_holds_and_ignores_everything_else(self):
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self.cache.want("B" * 64)
        with open(os.path.join(self.directory, "notes.txt"), "w") as handle:
            handle.write("x")
        self.assertEqual(self.cache.roots(), [REAL_ROOT_HEX])

    def test_the_cache_is_bounded_and_drops_the_oldest(self):
        # Bounded with a small limit rather than the real 64: each entry needs
        # genuine receive work, and the arithmetic is the same at any size.
        kept = 3
        roots = []
        with mock.patch.object(prework, "MAX_ENTRIES", kept):
            for index in range(kept + 2):
                root = "%064X" % index
                self.cache.put(root, work.solve_parallel(bytes.fromhex(root),
                                                         work.RECEIVE_THRESHOLD,
                                                         budget_seconds=180.0))
                roots.append(root)
        held = self.cache.roots()
        self.assertEqual(len(held), kept)
        self.assertEqual(sorted(held), sorted(roots[-kept:]),
                         "the oldest entries are the ones to drop")

    def test_dropping_what_is_not_there_says_so(self):
        self.assertFalse(self.cache.drop(REAL_ROOT_HEX))
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self.assertTrue(self.cache.drop(REAL_ROOT_HEX))


class Precomputing(_CacheCase):
    # At the RECEIVE threshold throughout: it is the cheapest real threshold,
    # and the cache refuses to store anything easier, which is the point of
    # `test_work_below_even_the_receive_threshold_is_not_stored`.
    RECEIVE = work.RECEIVE_THRESHOLD

    def test_it_stores_work_for_the_root_it_was_given(self):
        done = self.cache.precompute(REAL_ROOT_HEX, threshold=self.RECEIVE,
                                     budget_seconds=180.0)
        self.assertEqual(done["precomputed"], REAL_ROOT_HEX)
        self.assertFalse(done["already_had_it"])
        self.assertTrue(work.validates(REAL_ROOT, done["work"], self.RECEIVE))
        self.assertEqual(self.cache.get(REAL_ROOT_HEX, self.RECEIVE), done["work"])

    def test_it_takes_the_newest_pending_root_when_given_none(self):
        self.cache.want(REAL_ROOT_HEX)
        done = self.cache.precompute(threshold=self.RECEIVE, budget_seconds=180.0)
        self.assertEqual(done["precomputed"], REAL_ROOT_HEX)

    def test_nothing_pending_is_said_rather_than_guessed_at(self):
        self.assertEqual(self.cache.precompute(threshold=self.RECEIVE),
                         {"precomputed": None, "reason": "nothing is pending"})

    def test_work_already_held_is_not_searched_for_again(self):
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        done = self.cache.precompute(REAL_ROOT_HEX, threshold=work.SEND_THRESHOLD)
        self.assertTrue(done["already_had_it"])
        self.assertEqual(done["work"], SEND_WORK)

    def test_a_budget_that_runs_out_stores_nothing(self):
        with self.assertRaises(work.WorkUnavailable):
            self.cache.precompute(REAL_ROOT_HEX, threshold=work.SEND_THRESHOLD,
                                  budget_seconds=0.0, workers=2)
        self.assertFalse(self.cache.has(REAL_ROOT_HEX))


class TheCacheIsOffUnlessItIsConfigured(unittest.TestCase):
    def test_no_environment_variable_means_no_cache_at_all(self):
        for env in ({}, {prework.CACHE_ENV: ""}, {prework.CACHE_ENV: "   "}):
            self.assertIsNone(prework.WorkCache.from_env(env), env)

    def test_the_variable_names_the_directory(self):
        cache = prework.WorkCache.from_env({prework.CACHE_ENV: "/tmp/somewhere"})
        self.assertEqual(cache.directory, "/tmp/somewhere")

    def test_a_node_built_with_no_environment_has_no_cache(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            node = nanonode.HttpNanoNode("https://node.example/rpc")
        self.assertIsNone(node.prework)

    def test_passing_none_refuses_a_cache_the_environment_configured(self):
        with mock.patch.dict(os.environ, {prework.CACHE_ENV: "/tmp/x"}, clear=True):
            self.assertIsNotNone(nanonode.HttpNanoNode("https://n/rpc").prework)
            self.assertIsNone(nanonode.HttpNanoNode("https://n/rpc", prework=None).prework)


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ASendGetsItsWorkFromTheCacheAndAsksNoNode(_CacheCase):
    """The end-to-end claim: a node that refuses `work_generate` can still be paid through.

    The frontier is `REAL_ROOT_HEX`, so the block's work root is the root the
    send-grade vector was found for. The node below answers `account_info` and
    `process` and refuses `work_generate` the way a public node does.
    """

    REP = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"

    def _node(self, **kwargs):
        self.asked = []
        self.published = []

        def urlopen(request, timeout=None):
            body = json.loads(request.data.decode("utf-8"))
            self.asked.append(body["action"])
            if body["action"] == "account_info":
                return _Answer(json.dumps({
                    "frontier": REAL_ROOT_HEX, "balance": str(6 * ONE_XNO),
                    "confirmed_frontier": REAL_ROOT_HEX,
                    "confirmed_balance": str(6 * ONE_XNO),
                    "representative": self.REP, "confirmed_representative": self.REP,
                    "block_count": "7", "confirmed_height": "7",
                }).encode())
            if body["action"] == "work_generate":
                return _Answer(json.dumps({"error": "Work generation is disabled"}).encode())
            if body["action"] == "process":
                self.published.append(body["block"])
                return _Answer(json.dumps({"hash": "C" * 64}).encode())
            raise AssertionError("unexpected call: " + body["action"])

        patch = mock.patch.object(urllib.request, "urlopen", urlopen)
        patch.start()
        self.addCleanup(patch.stop)
        return nanonode.HttpNanoNode("https://node.example/rpc", **kwargs)

    def _send(self, node):
        keys = _keystore.KeyStore()
        address = keys.put(bytes(range(1, 33)))
        return payments.send(address, address, "1", "k1", node, keys, {},
                             env={"NANO_WALLET_ALLOW_SEND": "1"})

    def test_the_send_goes_out_carrying_the_precomputed_work(self):
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        node = self._node(prework=self.cache)
        self._send(node)
        self.assertEqual(len(self.published), 1)
        block = self.published[0]
        self.assertEqual(block["work"], SEND_WORK)
        self.assertTrue(work.validates(REAL_ROOT, block["work"], work.SEND_THRESHOLD),
                        "the block went out under-worked for a send")

    def test_the_node_is_never_asked_for_work_at_all(self):
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self._send(self._node(prework=self.cache))
        self.assertNotIn("work_generate", self.asked)
        self.assertEqual(self.asked, ["account_info", "process"])

    def test_without_the_cache_the_same_send_cannot_find_work(self):
        # The control. If this ever passes, the test above is not measuring
        # the cache.
        with self.assertRaises(payments.ToolError) as caught:
            self._send(self._node(prework=None))
        self.assertEqual(caught.exception.reason, "work_unavailable")
        self.assertEqual(self.published, [])

    def test_receive_grade_work_in_the_cache_does_not_let_a_send_out(self):
        self.cache.put(REAL_ROOT_HEX, RECEIVE_WORK)
        with self.assertRaises(payments.ToolError) as caught:
            self._send(self._node(prework=self.cache))
        self.assertEqual(caught.exception.reason, "work_unavailable")
        self.assertEqual(self.published, [])

    def test_a_cache_directory_that_vanished_is_a_miss_not_a_crash(self):
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        shutil.rmtree(self.directory)
        with self.assertRaises(payments.ToolError) as caught:
            self._send(self._node(prework=self.cache))
        self.assertEqual(caught.exception.reason, "work_unavailable")

    def test_the_published_block_becomes_the_next_root_to_work_on(self):
        # The hint is the hash the NODE reported, which is all `process` has -
        # `payments.send` pops the locally computed one before publishing. A
        # node that reports a hash that is not the account's new frontier
        # therefore poisons the hint, and that costs a wasted precompute and a
        # later cache miss, nothing more: `get` re-derives the difficulty
        # against the root the send path actually asks about, so a hint cannot
        # put work on a block it is not work for.
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        self._send(self._node(prework=self.cache))
        self.assertEqual(self.cache.pending(), ["C" * 64])

    def test_a_wallet_that_does_no_work_of_its_own_does_not_read_the_cache(self):
        # `local_work=False` says the node must supply the work. Work from the
        # cache is work this wallet did, so that switch covers it as well.
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        with self.assertRaises(payments.ToolError) as caught:
            self._send(self._node(prework=self.cache, local_work=False))
        self.assertEqual(caught.exception.reason, "work_unavailable")
        self.assertEqual(self.published, [])

    def test_a_hint_that_cannot_be_filed_never_fails_a_published_block(self):
        # `want` runs AFTER the money moved. An exception here would read to
        # the caller as a send that did not go out, and it did.
        self.cache.put(REAL_ROOT_HEX, SEND_WORK)
        node = self._node(prework=self.cache)
        with mock.patch.object(type(self.cache), "want",
                               side_effect=RuntimeError("disk on fire")):
            result = self._send(node)
        self.assertEqual(len(self.published), 1, "the block did not go out")
        self.assertEqual(len(result["block_hash"]), 64)
        self.assertEqual(self.published[0]["work"], SEND_WORK)

    def test_a_receive_still_falls_back_to_local_work_with_no_cache_entry(self):
        # The behaviour that shipped before this change must be untouched.
        node = self._node(prework=self.cache)
        found = node.work_generate(REAL_ROOT_HEX, "receive")
        self.assertTrue(work.validates(REAL_ROOT, found, work.RECEIVE_THRESHOLD))
        self.assertIn("work_generate", self.asked)


class WhichThresholdASubtypeNeeds(unittest.TestCase):
    def test_a_receive_and_an_open_need_the_lower_one(self):
        for subtype in ("receive", "open"):
            self.assertEqual(prework.threshold_for(subtype), work.RECEIVE_THRESHOLD)

    def test_everything_else_including_an_unknown_subtype_needs_the_send_one(self):
        # Guessing low here is the one mistake in this file that could put a
        # block on the network under-worked.
        for subtype in ("send", "change", "epoch", "", None, "RECEIVE", "receive "):
            self.assertEqual(prework.threshold_for(subtype), work.SEND_THRESHOLD, subtype)


if __name__ == "__main__":
    unittest.main()
