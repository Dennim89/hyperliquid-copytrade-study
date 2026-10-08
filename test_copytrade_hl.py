"""Offline unit tests for copytrade_hl (synthetic data only, no network)."""
import random, socket, unittest
from decimal import Decimal as D

import copytrade_hl as C

DAY = C.DAY_MS
T = 1_790_000_000_000


def pf(window_rows):
    """window -> [(ts, pnl, av)] -> portfolio-like API response (strings, like the API)."""
    return [[w, {'accountValueHistory': [[t, str(a)] for t, _, a in rows], 'pnlHistory': [[t, str(p)] for t, p, _ in rows],
                 'vlm': '0.0'}] for w, rows in window_rows.items()]


def daily(days, pnl=lambda i: 0, av=lambda i: 1000):
    """Samples at T - i days for i = days..0 (i = days is inception: pnl 0, av 0)."""
    rows = []
    for i in range(days, -1, -1):
        rows.append((T - i * DAY, 0 if i == days else pnl(i), 0 if i == days else av(i)))
    return rows


class Blocks(unittest.TestCase):
    def test_split_daily(self):
        s = C.series(pf({'perpAllTime': daily(200, pnl=lambda i: 200 - i)}), 'perpAllTime')
        for k in range(1, 5):   # pnl rises 1/day -> every 30-day block gains exactly 30
            st, dp, cap = C.acct_block(s, k)
            self.assertEqual((st, dp, cap), ('ok', D(30), D(1000)))

    def test_nearest_sample_and_ties(self):
        # samples every 7 days: boundary T-30d lies between T-28d and T-35d -> nearest is T-28d (2 d away)
        rows = [(T - i * DAY, D(i), D(100)) for i in range(140, -1, -7)]
        ts, pn, av = [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]
        self.assertEqual(ts[C.boundary(ts, av, T - 30 * DAY, 7 * DAY)], T - 28 * DAY)
        # exact tie (samples at b-1d and b+1d): earlier wins
        ts2, av2 = [T - 2 * DAY, T], [D(1), D(1)]
        self.assertEqual(C.boundary(ts2, av2, T - DAY, 7 * DAY), 0)
        # nearest farther than tolerance -> None
        self.assertIsNone(C.boundary([T - 40 * DAY, T], [D(1), D(1)], T - 20 * DAY, 7 * DAY))

    def test_absent_and_truncated(self):
        young = C.series(pf({'w': daily(10, pnl=lambda i: 5)}), 'w')   # created 10 days ago
        self.assertEqual(C.acct_block(young, 3)[0], 'absent')          # block 3 ended 30 days ago
        st, dp, _ = C.acct_block(young, 4)                               # block 4 starts before inception
        self.assertEqual((st, dp), ('ok', D(5)))
        trunc = C.series(pf({'w': [(T - 10 * DAY, 0, 500), (T, 5, 500)]}), 'w')   # first AV != 0: not inception
        self.assertEqual(C.acct_block(trunc, 3), ('invalid', 'sparse_or_truncated'))

    def test_roi_trapezoid_and_aggregation(self):
        # AV linear 1000 -> 3000 across block 4 (daily samples): time-weighted mean = 2000; pnl +300
        rows = [(T - i * DAY, D(300) * (30 - i) / 30, D(1000) + D(2000) * (30 - i) / 30) for i in range(30, -1, -1)]
        a = C.series(pf({'perpAllTime': [(T - 60 * DAY, 0, 0)] + rows}), 'perpAllTime')
        st, dp, cap = C.acct_block(a, 4)
        self.assertEqual(st, 'ok'); self.assertEqual(dp, D(300)); self.assertEqual(cap, D(2000))
        b = C.series(pf({'perpAllTime': daily(90, pnl=lambda i: -(90 - i), av=lambda i: 3000)}), 'perpAllTime')
        r, why = C.trader_block([a, b], 4)                               # master + one sub
        self.assertEqual(why, 'ok')
        self.assertEqual(r['dp'], D(300) - D(30)); self.assertEqual(r['cap'], D(5000))
        self.assertAlmostEqual(r['roi'], 270 / 5000)
        small = C.series(pf({'w': daily(90, pnl=lambda i: 90 - i, av=lambda i: 500)}), 'w')
        self.assertEqual(C.trader_block([small], 4)[1], 'capital<min')
        flat = C.series(pf({'w': daily(90, av=lambda i: 5000)}), 'w')
        self.assertEqual(C.trader_block([flat], 4)[1], 'inactive')
        self.assertEqual(C.trader_block([None], 4)[1], 'missing')


class Stats(unittest.TestCase):
    def test_spearman(self):
        self.assertAlmostEqual(C.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(C.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertAlmostEqual(C.spearman([1, 2, 2, 3], [1, 2, 3, 4]), 4.5 / (22.5 ** 0.5))   # average ranks for ties
        self.assertEqual(C.avg_ranks([5, 1, 5, 3]), [3.5, 1.0, 3.5, 2.0])

    def test_bootstrap_deterministic_and_ci(self):
        self.assertEqual(list(C.draws_idx(50, 3, 7)), list(C.draws_idx(50, 3, 7)))
        rng = random.Random(1)
        xa = [rng.random() for _ in range(300)]
        noise = [rng.random() for _ in range(300)]
        rep0 = C.pair_report([(str(i), xa[i], noise[i]) for i in range(300)], draws=300)
        self.assertLess(rep0['spearman_ci']['lo'], 0); self.assertGreater(rep0['spearman_ci']['hi'], 0)
        rep1 = C.pair_report([(str(i), xa[i], xa[i] + 0.3 * noise[i]) for i in range(300)], draws=300)
        self.assertGreater(rep1['spearman_ci']['lo'], 0.5)
        self.assertEqual(rep1, C.pair_report([(str(i), xa[i], xa[i] + 0.3 * noise[i]) for i in range(300)], draws=300))

    def test_deciles_and_transition(self):
        x = list(range(100)); keys = [str(i) for i in x]
        self.assertEqual(C.deciles(x, keys), [i // 10 for i in x])
        m = C.transition(x, x, keys)
        self.assertEqual([m[i][i] for i in range(10)], [10] * 10)
        m2 = C.transition(x, [-v for v in x], keys)
        self.assertEqual([m2[i][9 - i] for i in range(10)], [10] * 10)
        s = C.pair_stats([float(v) for v in x], [1.0 if v >= 90 else -1.0 for v in x], keys)
        self.assertEqual((s['n_top'], s['share_pos_top'], s['base_pos']), (10, 1.0, 0.1))
        self.assertAlmostEqual(s['excess'], 0.9)
        self.assertEqual(C.deciles([1.0] * 20, ['%02d' % i for i in range(20)]), [i // 2 for i in range(20)])  # ties by key

    def test_universe_selection(self):
        rows = [{'addr': '0x%02d' % i, 'pnl_all': D(100 - i), 'pnl_month': D(0)} for i in range(40)]
        top, ctrl, order = C.candidates(C.pit_ranking(rows), 5, 4)
        self.assertEqual(top, ['0x00', '0x01', '0x02', '0x03', '0x04'])
        self.assertEqual(ctrl, ['0x05', '0x15', '0x25', '0x35'])      # floor((i+0.5)*40/4)
        self.assertEqual(order[:4], ['0x00', '0x05', '0x01', '0x15'])
        self.assertEqual(C.choose_k([0, 0, 2, 5, 20], budget=6), 4)     # k=2..4 need 1+1+3 = 5 <= 6; k=5 needs 11
        self.assertEqual(C.choose_k([0, 0, 2, 5, 20], budget=11), 10)   # the 20-sub trader stays excluded (kmax 10)
        self.assertEqual(C.choose_k([3], budget=2), 2)                   # the 3-sub trader is simply excluded
        self.assertIsNone(C.choose_k([0, 0, 0], budget=2))               # even k=0 does not fit


def fill(t, side, sz, px, start, coin='BTC', tid=None, fee='0'):
    return {'coin': coin, 'time': t, 'side': side, 'sz': str(sz), 'px': str(px), 'startPosition': str(start),
            'dir': 'x', 'tid': tid if tid is not None else t, 'fee': fee}


class Positions(unittest.TestCase):
    def test_open_add_close(self):
        f = [fill(1000, 'B', 1, 100, 0), fill(2000, 'B', 1, 110, 1), fill(3000, 'A', 2, 120, 2)]
        eps = C.episodes(f)
        self.assertEqual(len(eps), 1)
        ep = eps[0]
        self.assertTrue(ep['closed']); self.assertEqual((ep['side'], ep['t_open'], ep['t_close']), (1, 1000, 3000))
        pnl, notional, fees = C.leader_episode(ep, fee=D(0))
        self.assertEqual((pnl, notional), (D(30), D(210)))

    def test_flip_censor_gap_spot(self):
        f = [fill(1000, 'B', 2, 100, 0), fill(2000, 'A', 3, 90, 2),          # long 2 -> short 1 (flip)
             fill(3000, 'B', 1, 80, -1),                                      # short closed
             fill(500, 'A', 1, 50, 4, coin='ETH'),                            # ETH starts at +4: left-censored
             fill(900, 'B', 1, 1, 0, coin='@107'),                            # spot: ignored
             fill(4000, 'B', 1, 10, 0, coin='SOL'), fill(5000, 'B', 1, 11, 7, coin='SOL')]   # SOL gap: broken
        eps = C.episodes(f)
        btc = [e for e in eps if e['coin'] == 'BTC']
        self.assertEqual([(e['side'], e['closed']) for e in btc], [(1, True), (-1, True)])
        self.assertEqual(btc[0]['legs'], [(1000, D(2), D(100)), (2000, D(-2), D(90))])
        self.assertEqual(btc[1]['legs'], [(2000, D(-1), D(90)), (3000, D(1), D(80))])
        eth = [e for e in eps if e['coin'] == 'ETH']
        self.assertTrue(eth[0]['left_censored'])
        self.assertFalse(any(e['coin'] == '@107' for e in eps))
        sol = [e for e in eps if e['coin'] == 'SOL']
        self.assertTrue(sol[0].get('broken'))
        self.assertTrue(sol[1]['left_censored'])


class Follower(unittest.TestCase):
    def prices(self):
        p = C.Prices()
        base = 1_000_000_020_000 - 1_000_000_020_000 % 60000
        p.add('BTC', '1m', [{'t': base + i * 60000, 'c': str(100 + i)} for i in range(120)])
        return p, base

    def test_delay_and_fees(self):
        p, b = self.prices()
        ep = {'coin': 'BTC', 'legs': [(b + 5000, D(2), D(99)), (b + 30 * 60000 + 5000, D(-2), D(150))]}
        pnl, notional, fees, ivs = C.follow_episode(ep, p, 0)
        # entry at close of minute 0 (100), exit at close of minute 30 (130): gross 60, fee 0.00045*(200+260)
        self.assertEqual((notional, fees), (D(200), D('0.20700')))
        self.assertEqual(pnl, D(60) - D('0.20700'))
        self.assertIsInstance(pnl, D)
        pnl60, n60, f60, _ = C.follow_episode(ep, p, 60000)                 # one candle later on both legs
        self.assertEqual((n60, pnl60), (D(202), D(60) - D('0.00045') * (202 + 262)))
        self.assertEqual(ivs, {'1m'})
        self.assertIsNone(C.follow_episode(ep, p, 200 * 60000))              # no candle -> unpriced
        short = {'coin': 'BTC', 'legs': [(b + 5000, D(-1), D(1)), (b + 10 * 60000, D(1), D(1))]}
        self.assertEqual(C.follow_episode(short, p, 0, fee=D(0))[0], D(100) - D(110))

    def test_interval_fallback(self):
        p = C.Prices()
        p.add('ETH', '15m', [{'t': 0, 'c': '10'}, {'t': 900000, 'c': '12'}])
        self.assertEqual(p.price('ETH', 899999), (D(10), '15m'))
        p.add('ETH', '1m', [{'t': 900000, 'c': '11'}])
        self.assertEqual(p.price('ETH', 900001), (D(11), '1m'))
        self.assertEqual(p.price('ETH', 960000), (D(12), '15m'))

    def test_ratio_boot(self):
        r = C.ratio_boot({'a': (D(10), D(1000)), 'b': (D(-5), D(1000))}, draws=200)
        self.assertEqual(r['pnl_per_unit_notional'], 0.0025)
        self.assertLessEqual(r['ci']['lo'], 0.0025); self.assertGreaterEqual(r['ci']['hi'], 0.0025)


class Selection(unittest.TestCase):
    def test_followed_traders_oos(self):
        # 40 traders: ROI in block 2 and 3 increasing with index -> top decile (4) in both = the last four;
        # block-4 values are poison and must not influence selection
        tr = {}
        for i in range(40):
            b = {k: {'roi': float(i) if k in (2, 3) else -float(i), 'dp': D(i), 'cap': D(1000)} for k in (1, 2, 3, 4)}
            tr['0x%02d' % i] = {'excluded': None, 'groups': ['top'], 'subs': [], 'blocks': {'perp': b, 'total': b}}
        self.assertEqual(C.followed_traders(tr), ['0x39', '0x38', '0x37', '0x36'])

    def test_paged_fills(self):
        class FakeStore:
            def __init__(self): self.calls = []
            def get(self, rel, url, body):
                self.calls.append(body['startTime'])
                s = body['startTime']
                if s == 0: return [{'tid': i, 'oid': i, 'side': 'B', 'time': i // 2, 'coin': 'BTC'} for i in range(2000)]
                return [{'tid': i, 'oid': i, 'side': 'B', 'time': i // 2, 'coin': 'BTC'} for i in range(1998, 2100)]
        st = FakeStore()
        out = C.paged(st, 'userFillsByTime', '0xabc', 0, 10 ** 6, 2000)
        self.assertEqual(st.calls, [0, 999])                 # next page starts at the last returned time
        self.assertEqual(len(out), 2100)                     # overlap at t=999 de-duplicated by tid


class Offline(unittest.TestCase):
    def test_block_network(self):
        saved = (socket.socket.connect, socket.create_connection, socket.getaddrinfo)
        try:
            C.block_network()
            with self.assertRaises(RuntimeError): socket.create_connection(('api.hyperliquid.xyz', 443))
            with self.assertRaises(RuntimeError): socket.getaddrinfo('api.hyperliquid.xyz', 443)
        finally:
            socket.socket.connect, socket.create_connection, socket.getaddrinfo = saved

    def test_weights(self):
        self.assertEqual(C.weight({'type': 'portfolio'}, 0), 20)
        self.assertEqual(C.weight({'type': 'clearinghouseState'}, 0), 2)
        self.assertEqual(C.weight({'type': 'userFillsByTime'}, 2000), 120)
        self.assertEqual(C.weight({'type': 'candleSnapshot'}, 5000), 20 + 84)


if __name__ == '__main__':
    unittest.main()
