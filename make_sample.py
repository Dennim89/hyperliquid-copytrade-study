"""Synthetic sample for copytrade_hl.py: made-up accounts, prices and fills. No network.

The study's own fetch code (copytrade_hl.fetch_all) runs against a fake Hyperliquid API inside this process, so
<dir>/raw gets the layout of a real run: gzip responses with .meta.json files, a frozen protocol, request weights.
A virtual clock stands in for the rate-limit pauses, so a run takes seconds and gives the same bytes every time.

  python make_sample.py demo                    # world 'null': no lasting skill
  python make_sample.py demo2 --world skill     # world 'skill': lasting skill, so the gate passes and step 3 runs
  python copytrade_hl.py --offline --root demo  # rebuild the report from demo/raw with sockets blocked

Nothing here describes real traders or real markets. The two worlds exist to exercise the pipeline.
"""
import argparse, email.utils, json, math, os, random, socket, time, urllib.request

import copytrade_hl as C

NOW0 = 1_767_225_600_000            # 2026-01-01T00:00Z: the virtual clock starts here
DAY, HOUR, MIN = C.DAY_MS, 3_600_000, 60_000
API = 'https://synthetic.invalid/info'            # .invalid never resolves; the reports name it as the data source
LB_URL = 'https://synthetic.invalid/leaderboard'
B = [NOW0 - (4 - k) * 30 * DAY for k in range(5)]  # block k (1..4) runs from B[k-1] to B[k]

WORLDS = {
    # market: return factor per 30-day block (oldest first); each trader has a beta ~ N(0, 1) to it.
    # gamblers: share of accounts with huge monthly swings.  skill_sd: spread of a lasting monthly ROI per trader.
    'null': {'market': (0.0, 0.08, -0.08, -0.08), 'gamblers': 0.15, 'skill_sd': 0.0},
    'skill': {'market': (0.0, 0.08, -0.08, -0.08), 'gamblers': 0.03, 'skill_sd': 0.15},
}
SPOT_MARKET = (0.04, 0.10, -0.06, 0.05)
COINS = {'BTC': 60000.0, 'ETH': 3000.0}
P_START, P_END = NOW0 - 32 * DAY, NOW0 + 6 * HOUR   # synthetic 1-minute price path
IV = dict(C.Prices.IV)
FUNDING_RATE = 0.0000125


def accrue(t, a, b, amount, start):
    """Part of `amount` (earned evenly over [a, b]) earned by time t, for an account that exists from `start`;
    the last block keeps its pace past b (the fetch runs a few minutes after the virtual 'now')."""
    lo = max(a, start)
    if t <= lo or lo >= b: return 0.0
    return amount * (t - lo) / (b - a)


class Account:
    def __init__(self, acct, rng, cap, spot, rois, spot_rois, perp_pre, spot_pre, start, skill, vlm_month):
        self.acct, self.cap, self.spot, self.rois, self.spot_rois = acct, cap, spot, rois, spot_rois
        self.perp_pre, self.spot_pre, self.start, self.skill, self.vlm_month = perp_pre, spot_pre, start, skill, vlm_month
        self.phase = rng.uniform(0, 11 * DAY)

    def _pnl(self, t, cap, rois, pre):
        if t <= self.start: return 0.0
        x = accrue(min(t, B[0]), self.start, B[0], pre, self.start) if self.start < B[0] else 0.0
        for k in range(4):
            end = B[k + 1] if k < 3 else max(B[4], t)
            x += accrue(min(t, end), B[k], B[k + 1], rois[k] * cap, self.start)
        return x

    def perp_pnl(self, t): return self._pnl(t, self.cap, self.rois, self.perp_pre)
    def total_pnl(self, t): return self.perp_pnl(t) + self._pnl(t, self.spot, self.spot_rois, self.spot_pre)

    def perp_av(self, t):
        return 0.0 if t <= self.start else self.cap * (1 + 0.1 * math.sin(2 * math.pi * (t + self.phase) / (11 * DAY)))

    def total_av(self, t):
        return 0.0 if t <= self.start else self.perp_av(t) + self.spot * (1 + 0.05 * math.sin(2 * math.pi * t / (13 * DAY)))


class World:
    def __init__(self, name='null', seed=7, n_rows=1000):
        w, rng = WORLDS[name], random.Random('world/%s/%d' % (name, seed))
        self.name, self.seed, self.masters, self.accounts = name, seed, [], {}
        for i in range(n_rows):
            addr = '0xsyn%05d' % i
            size = 10 ** rng.uniform(0.5, 7.5)
            kind = rng.choices(['perp', 'spot', 'idle'], [0.55, 0.30, 0.15])[0]
            age = rng.uniform(150, 900) if rng.random() < 0.85 else rng.uniform(10, 150)
            vol = rng.uniform(0.8, 2.5) if rng.random() < w['gamblers'] else rng.uniform(0.03, 0.12)
            beta, skill = rng.gauss(0, 1), rng.gauss(0, w['skill_sd']) if w['skill_sd'] else 0.0
            if kind == 'idle': skill, beta, vol = 0.0, 0.0, 0.0
            rois = [skill + beta * m + vol * rng.gauss(0, 1) for m in w['market']]
            spot_rois = [m + 0.03 * rng.gauss(0, 1) for m in SPOT_MARKET]
            perp = size * rng.uniform(0.4, 1.0) if kind == 'perp' else size if kind == 'idle' else 0.0
            spot = size - perp if kind == 'perp' else size if kind == 'spot' else 0.0
            pre = size * (rng.uniform(0, 4) if kind == 'spot' else rng.uniform(-0.3, 2.0) if kind == 'perp' else 0.0)
            u = rng.random()
            n_subs = 0 if u < 0.85 or kind != 'perp' else rng.randint(1, 3) if u < 0.95 else rng.randint(4, 9) if u < 0.99 else rng.randint(12, 40)
            weights = [1.0] + [rng.uniform(0.2, 1.0) for _ in range(n_subs)]
            ids = [addr] + ['%ss%d' % (addr, j + 1) for j in range(n_subs)]
            start = NOW0 - int(age * DAY)
            turnover = rng.uniform(2, 40) if kind == 'perp' else rng.uniform(0, 1) if kind == 'spot' else 0.0
            accts = []
            for j, (acct, wt) in enumerate(zip(ids, weights)):
                share = wt / sum(weights)
                r = rois if j == 0 else [x + 0.02 * rng.gauss(0, 1) for x in rois]
                a = Account(acct, rng, perp * share, spot if j == 0 else 0.0, r, spot_rois,
                            pre * share if kind != 'spot' else 0.0, pre if kind == 'spot' and j == 0 else 0.0,
                            start, skill, (perp * share + (spot if j == 0 else 0.0)) * turnover)
                self.accounts[acct] = a; accts.append(a)
            self.masters.append((addr, accts))
        self.subs = {addr: [a.acct for a in accts[1:]] for addr, accts in self.masters}
        self.prices = {c: self._path(c, p0) for c, p0 in COINS.items()}
        self._eps = {}

    # ---- prices: a random walk per coin with a 20-minute drift ('move') once in every 6-hour slot ----
    def _path(self, coin, p0):
        rng = random.Random('prices/%s/%s/%d' % (coin, self.name, self.seed))
        n = (P_END - P_START) // MIN
        drift, moves = [0.0] * n, []
        for slot in range(P_START, P_END - 6 * HOUR, 6 * HOUR):
            t = slot + rng.randint(5, 60) * MIN
            d = rng.choice((1, -1))
            moves.append((t, d))
            for i in range(20): drift[(t - P_START) // MIN + i] = d * 0.004 / 20
        closes, x = [], math.log(p0)
        for i in range(n):
            x += drift[i] + rng.gauss(0, 0.0004)
            closes.append(math.exp(x))
        return {'closes': closes, 'moves': moves}

    def px(self, coin, t): return self.prices[coin]['closes'][(t - P_START) // MIN]

    def candles(self, coin, iv, start, end, now):
        ms, out = IV[iv], []
        t = max(-(-start // ms) * ms, now - now % ms - 4999 * ms, P_START + ms)
        while t <= min(end, now) and t + ms <= P_END and len(out) < 5000:
            c = self.px(coin, t + ms - MIN)
            out.append({'t': t, 'T': t + ms - 1, 's': coin, 'i': iv, 'o': '%.2f' % self.px(coin, t - MIN), 'c': '%.2f' % c})
            t += ms
        return out

    # ---- fills: leaders trade some of the moves; skill sets how often they pick the right side ----
    def episodes(self, acct):
        if acct not in self._eps:
            a, rng = self.accounts[acct], random.Random('fills/%s/%s/%d' % (acct, self.name, self.seed))
            p_right = min(0.75, max(0.25, 0.5 + 1.5 * a.skill))
            fills, funding, tid = [], [], 0
            for coin in sorted(COINS):
                for t_move, d in self.prices[coin]['moves']:
                    if not (NOW0 - 30 * DAY + HOUR <= t_move <= NOW0 - 6 * HOUR) or rng.random() > 0.35: continue
                    side = d if rng.random() < p_right else -d
                    t0 = t_move - rng.randint(5, 55) * 1000
                    t1 = t0 + rng.randint(60, 240) * MIN
                    p0, p1 = self.px(coin, t0), self.px(coin, t1)
                    sz = '%.5f' % (a.cap * rng.uniform(0.5, 3.0) / p0)
                    if float(sz) == 0: continue
                    for t, px, opening in ((t0, p0, True), (t1, p1, False)):
                        tid += 1
                        buy = (side > 0) == opening
                        fills.append({'coin': coin, 'px': '%.2f' % px, 'sz': sz, 'side': 'B' if buy else 'A', 'time': t,
                                      'startPosition': '0' if opening else ('-' if side < 0 else '') + sz,
                                      'dir': ('Open ' if opening else 'Close ') + ('Long' if side > 0 else 'Short'),
                                      'fee': '%.6f' % (float(sz) * px * 0.00045), 'feeToken': 'USDC', 'tid': tid, 'oid': tid,
                                      'crossed': True})
                    for h in range(t0 - t0 % HOUR + HOUR, t1 + 1, HOUR):
                        funding.append({'time': h, 'delta': {'type': 'funding', 'coin': coin, 'fundingRate': str(FUNDING_RATE),
                                                             'szi': ('-' if side < 0 else '') + sz,
                                                             'usdc': '%.6f' % (-side * float(sz) * self.px(coin, h) * FUNDING_RATE)}})
            self._eps[acct] = (sorted(fills, key=lambda f: (f['time'], f['tid'])), sorted(funding, key=lambda f: f['time']))
        return self._eps[acct]

    # ---- the fake API ----
    def leaderboard(self, t):
        rows = []
        for addr, accts in self.masters:
            tot = lambda x: sum(a.total_pnl(x) for a in accts)
            av = sum(a.total_av(t) for a in accts)
            vm = sum(a.vlm_month for a in accts) if t > accts[0].start else 0.0
            age_m = max(0.0, (t - accts[0].start) / (30 * DAY))
            wp = []
            for name, days in (('day', 1), ('week', 7), ('month', 30), ('allTime', None)):
                pnl = tot(t) - (tot(t - days * DAY) if days else 0.0)
                wp.append([name, {'pnl': '%.2f' % pnl, 'roi': '%.6f' % (pnl / max(av, 1.0)),
                                  'vlm': '%.2f' % (vm * (days / 30 if days else age_m))}])
            rows.append({'ethAddress': addr, 'accountValue': '%.2f' % av, 'windowPerformances': wp, 'prize': 0, 'displayName': None})
        return {'leaderboardRows': rows}

    def portfolio(self, acct, now):
        a, rng = self.accounts[acct], random.Random('samples/%s/%s/%d' % (acct, self.name, self.seed))
        lo = max(a.start, NOW0 - 200 * DAY)
        ts = [now]
        while ts[-1] - lo > 2 * DAY: ts.append(ts[-1] - int(rng.uniform(2, 7) * DAY))
        ts = sorted([x for x in ts if x > lo] + [lo])
        mts = [x for x in range(now - 30 * DAY, now + 1, DAY) if x >= a.start]
        def win(times, pnl, av, base=0.0):
            return {'accountValueHistory': [[x, '%.2f' % av(x)] for x in times],
                    'pnlHistory': [[x, '%.2f' % (pnl(x) - base)] for x in times], 'vlm': '0.0'}
        return [['month', win(mts, a.total_pnl, a.total_av, a.total_pnl(mts[0]) if mts else 0.0)],
                ['allTime', win(ts, a.total_pnl, a.total_av)],
                ['perpAllTime', win(ts, a.perp_pnl, a.perp_av)]]

    def answer(self, url, body, now):
        if url == LB_URL: return self.leaderboard(now)
        if url != API: raise RuntimeError('synthetic API: unknown url ' + url)
        typ = body['type']
        if typ == 'subAccounts':
            subs = self.subs[body['user']]
            return [{'name': 'sub%d' % i, 'subAccountUser': s, 'master': body['user']} for i, s in enumerate(subs, 1)] or None
        if typ == 'portfolio': return self.portfolio(body['user'], now)
        if typ in ('userFillsByTime', 'userFunding'):
            rows = self.episodes(body['user'])[0 if typ == 'userFillsByTime' else 1]
            rows = [x for x in rows if body['startTime'] <= x['time'] <= body['endTime']]
            return rows[:2000 if typ == 'userFillsByTime' else 500]
        if typ == 'candleSnapshot':
            q = body['req']
            return self.candles(q['coin'], q['interval'], q['startTime'], q['endTime'], now)
        raise RuntimeError('synthetic API: unknown request type ' + typ)


class _Response:
    def __init__(self, data, headers): self._data, self.status, self.headers = data, 200, headers
    def read(self): return self._data
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def generate(root, world='null', seed=7, n_rows=1000, log=None):
    """Write <root>/raw for a synthetic world by running copytrade_hl.fetch_all against the fake API."""
    raw = os.path.join(root, 'raw')
    if os.path.exists(raw): raise FileExistsError('%s exists; use an empty folder' % raw)
    w, clock = World(world, seed, n_rows), [NOW0 / 1000.0]

    def urlopen(req, timeout=None):
        now = int(clock[0] * 1000)
        body = json.loads(req.data) if req.data else None
        data = json.dumps(w.answer(req.full_url, body, now), separators=(',', ':')).encode()
        hdr = {'Date': email.utils.formatdate(now / 1000, usegmt=True)}
        if req.full_url == LB_URL: hdr['Last-Modified'] = email.utils.formatdate((NOW0 - 40 * MIN) / 1000, usegmt=True)
        return _Response(data, hdr)

    def advance(s): clock[0] += s

    saved = (time.time, time.sleep, urllib.request.urlopen, C.API, C.LB_URL,
             socket.socket.connect, socket.create_connection, socket.getaddrinfo)
    try:
        C.block_network()                            # the fake API needs no sockets; prove it
        time.time, time.sleep, urllib.request.urlopen = (lambda: clock[0]), advance, urlopen
        C.API, C.LB_URL = API, LB_URL
        C.fetch_all(raw, log or (lambda s: None))
    finally:
        (time.time, time.sleep, urllib.request.urlopen, C.API, C.LB_URL,
         socket.socket.connect, socket.create_connection, socket.getaddrinfo) = saved
    return raw


def main():
    ap = argparse.ArgumentParser(description='Write a synthetic raw/ sample for copytrade_hl.py (no network).')
    ap.add_argument('dir', help='output folder; <dir>/raw must not exist yet')
    ap.add_argument('--world', choices=sorted(WORLDS), default='null')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--rows', type=int, default=1000, help='leaderboard rows (at least 400)')
    a = ap.parse_args()
    raw = generate(a.dir, a.world, a.seed, a.rows, log=lambda s: print('  fetch:', s))
    n = sum(len(fs) for _, _, fs in os.walk(raw))
    print('wrote %d files to %s (synthetic world %r, seed %d)' % (n, raw, a.world, a.seed))
    print('next: python copytrade_hl.py --offline --root %s' % a.dir)


if __name__ == '__main__':
    main()
