"""End-to-end tests: synthetic samples from make_sample.py, and the published result file. No network.

The synthetic runs go through the study's own fetch code against an in-process fake API with sockets blocked,
then rebuild the report offline. Bootstrap draws are cut from 2000 to 200 here to keep the suite fast.
"""
import contextlib, hashlib, io, json, os, re, shutil, socket, sys, tempfile, unittest
from unittest import mock

import copytrade_hl as C
import make_sample as S

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(HERE, 'results', 'copytrade_hl_20260925.json')
PARAM_KEYS = {'min_order_usd', 'min_capital_usd', 'capital_usd'}     # dollar amounts that are study parameters


@contextlib.contextmanager
def fast_bootstrap(draws=200):
    full = C.draws_idx
    C.draws_idx = lambda n, d=C.DRAWS, seed=C.SEED: full(n, min(d, draws), seed)
    try: yield
    finally: C.draws_idx = full


@contextlib.contextmanager
def no_network():
    saved = (socket.socket.connect, socket.create_connection, socket.getaddrinfo)
    C.block_network()
    try: yield
    finally: socket.socket.connect, socket.create_connection, socket.getaddrinfo = saved


def keys_with_dollars(o, path=''):
    """Paths of keys that name a dollar amount and are not study parameters."""
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if 'usd' in k.lower() and k not in PARAM_KEYS: out.append(path + '.' + k)
            out += keys_with_dollars(v, path + '.' + k)
    elif isinstance(o, list):
        for i, v in enumerate(o): out += keys_with_dollars(v, '%s[%d]' % (path, i))
    return out


def tree_digest(root):
    h = hashlib.sha256()
    for dp, dns, fs in os.walk(root):
        dns.sort()
        for f in sorted(fs):
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, root).encode())
            with open(p, 'rb') as fh: h.update(fh.read())
    return h.hexdigest()


class SyntheticWorld(unittest.TestCase):
    world = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='hl-%s-' % cls.world)
        with no_network():
            S.generate(cls.tmp, cls.world)
            with fast_bootstrap(): cls.rep = C.build_report(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class NullWorld(SyntheticWorld):
    """No lasting skill, a market factor that flips sign: the gate must stop the study."""
    world = 'null'

    def test_gate_stops_the_study(self):
        r = self.rep
        self.assertTrue(r['protocol_matches_frozen_copy'])
        self.assertTrue(r['gate']['enough_n'])
        self.assertFalse(r['gate']['pass'])
        self.assertFalse(r['gate']['excess_ci_lo_gt_0'])
        self.assertEqual(r['verdict']['class'], 'NO-GO')
        self.assertTrue(r['followability']['skipped'])
        self.assertEqual(r['leaderboard']['url'], S.LB_URL)

    def test_raw_layout_like_a_real_run(self):
        q, u = self.rep['requests'], self.rep['universe']
        self.assertEqual((u['leaderboard_rows'], u['top_n'], u['control_n']), (1000, 200, 200))
        self.assertEqual(q['by_type']['leaderboard'], 1)
        self.assertEqual(q['by_type']['subAccounts'], u['union'])
        self.assertGreaterEqual(q['by_type']['portfolio'], u['union'] - sum(g.get('subs>K', 0) for g in u['status_by_group'].values()))
        self.assertEqual((q['http_429'], q['failed_keys']), (0, 0))
        self.assertEqual(q['api_requests_total'], sum(q['by_type'].values()))
        self.assertTrue(any('subs>K' in g for g in u['status_by_group'].values()))   # traders with > K subs are left out

    def test_report_has_aggregates_only(self):
        text = json.dumps(self.rep)
        self.assertNotIn('0xsyn', text)                                  # no account id anywhere in the report
        self.assertEqual(keys_with_dollars(self.rep), [])

    def test_offline_cli_writes_then_verifies(self):
        out = os.path.join(self.tmp, 'report.json')
        argv = ['copytrade_hl.py', '--offline', '--root', self.tmp, '--out', out]
        with no_network(), fast_bootstrap(), mock.patch.object(sys, 'argv', argv), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            C.main()                                                      # first run writes the report
            with self.assertRaises(SystemExit) as ex:
                C.main()                                                  # second run rebuilds and compares bytes
        self.assertEqual(ex.exception.code, 0)
        self.assertIn(': MATCH (sha256', buf.getvalue())
        self.assertIn('verdict: NO-GO', buf.getvalue())

    def test_generation_is_deterministic(self):
        tmp2 = tempfile.mkdtemp(prefix='hl-again-')
        try:
            S.generate(tmp2, self.world)
            self.assertEqual(tree_digest(os.path.join(tmp2, 'raw')), tree_digest(os.path.join(self.tmp, 'raw')))
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_existing_raw_is_not_overwritten(self):
        with self.assertRaises(FileExistsError): S.generate(self.tmp, self.world)


class SkillWorld(SyntheticWorld):
    """Positive control: lasting skill built in. The gate must pass and step 3 (the follower) must run."""
    world = 'skill'

    def test_gate_passes_and_follower_runs(self):
        r = self.rep
        self.assertTrue(r['gate']['pass'])
        f = r['followability']
        self.assertFalse(f['skipped'])
        self.assertGreater(f['followed_traders_n'], 0)
        self.assertGreater(f['episodes_priced_all_delays'], 0)
        by = f['follower_by_delay_s']
        self.assertEqual(sorted(by, key=int), ['0', '10', '60', '300', '1800'])
        # the synthetic leaders trade 20-minute moves: a 30-minute delay must cost the follower most of the gain
        self.assertGreater(by['0']['pnl_per_unit_notional'], by['1800']['pnl_per_unit_notional'])
        self.assertIn(r['verdict']['class'], ('NO-GO', 'INSUFFICIENT', 'POSITIVE_BUT_TOO_SMALL', 'POSITIVE'))
        self.assertNotIn('0xsyn', json.dumps(r))


class PublishedResult(unittest.TestCase):
    """results/copytrade_hl_20260925.json: the study's report, cut to aggregates."""

    @classmethod
    def setUpClass(cls):
        with open(RESULT, encoding='utf-8') as f: cls.r = json.load(f)

    def test_protocol_is_the_frozen_one(self):
        self.assertTrue(self.r['protocol_matches_frozen_copy'])
        self.assertEqual(self.r['protocol'], C.PROTOCOL)                  # the published code runs the frozen protocol

    def test_verdict_and_gate(self):
        g, h = self.r['gate'], self.r['headline']['primary_perp_roi_A3_B4']
        self.assertEqual(self.r['verdict']['class'], 'NO-GO')
        self.assertEqual((g['n'], g['rho_ci_lo_gt_0'], g['excess_ci_lo_gt_0'], g['pass']), (99, True, False, False))
        self.assertEqual((h['top_decile_positive_B'], h['all_positive_B']), ('6/9', '46/99'))
        self.assertAlmostEqual(h['rho'], 0.535807)
        self.assertLess(h['excess_ci'][0], 0)
        self.assertTrue(self.r['followability']['skipped'])

    def test_aggregates_only(self):
        text = json.dumps(self.r)
        for bad in ('top_decile_traders', '<EVM', 'cex_copy_trading'):
            self.assertNotIn(bad, text)
        self.assertIsNone(re.search(r'0x[0-9a-fA-F]{6}', text))          # no account address
        self.assertEqual(keys_with_dollars(self.r), [])

    def test_summary_lines(self):
        lines = C.headline_lines(self.r)
        self.assertIn('verdict: NO-GO (persistence gate failed)', lines)
        self.assertTrue(any('+0.54' in x and '+0.34' in x and '+0.70' in x for x in lines))


if __name__ == '__main__':
    unittest.main()
