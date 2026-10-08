"""Hyperliquid copy-trading research (public data only, read-only; no orders, wallets, keys or signing).

Question: do publicly ranked perp traders keep their profitability out of sample, and would a delayed
follower still make money after fees?  Stages: universe (point-in-time leaderboard) -> ROI persistence
across 30-day blocks (the gate) -> followability (only when the gate passes) -> scale.

  python copytrade_hl.py --fetch-docs     # online: the cited docs pages -> raw/docs (quotes re-verified offline)
  python copytrade_hl.py --fetch          # online: sequential, weight-paced (70% of the documented limit), raw/
  python copytrade_hl.py --offline        # zero network: builds the report from raw/, or verifies an existing one
  python copytrade_hl.py --summary FILE   # print the headline of a report

--root (default: $HL_STUDY_ROOT, else this file's folder) is the folder that holds raw/ and the report.
The report keeps aggregates only: no account addresses, per-account rows or dollar totals.
"""
import argparse, bisect, gzip, hashlib, html, json, math, os, random, re, socket, sys, time, urllib.error, urllib.request
from decimal import Decimal as D

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get('HL_API_URL', 'https://api.hyperliquid.xyz/info')
LB_URL = os.environ.get('HL_LEADERBOARD_URL', 'https://stats-data.hyperliquid.xyz/Mainnet/leaderboard')
OUT_NAME = 'copytrade_hl_report.json'
UA = 'copytrade-hl-research/1.0 (read-only public data)'
GB = 'https://hyperliquid.gitbook.io/hyperliquid-docs/'

# ---- documented limits; every quote is re-checked offline against raw/docs ----------------------------
DOC_PAGES = {
    'rate_limits': (GB + 'for-developers/api/rate-limits-and-user-limits', [
        'The following rate limits apply per IP address: REST requests share an aggregated weight limit of 1200 per minute.',
        'The following info requests have weight 2: l2Book, allMids, clearinghouseState, orderStatus, spotClearinghouseState, exchangeStatus.',
        'All other documented info requests have weight 20.',
        'The following info endpoints have an additional rate limit weight per 20 items returned in the response: '
        'recentTrades, historicalOrders, userFills, userFillsByTime, fundingHistory, userFunding',
        'The candleSnapshot info endpoint has an additional rate limit weight per 60 items returned in the response.']),
    'fees': (GB + 'trading/fees', [
        'Perps fee tiers Base rate', 'Tier 14d weighted volume ($) Taker Maker Taker Maker Taker Maker Taker Maker '
        'Taker Maker Taker Maker Taker Maker 0 0.045% 0.015%']),
    'info_endpoint': (GB + 'for-developers/api/info-endpoint', [
        'Responses that take a time range will only return 500 elements or distinct blocks of data.',
        'Returns at most 2000 fills per response and only the 10000 most recent fills are available',
        'Only the most recent 5000 candles are available']),
    'error_responses': (GB + 'for-developers/api/error-responses', ['Order must have minimum value of $10.']),
    'contract_specs': (GB + 'trading/contract-specifications', ['funding payments every hour']),
}
W_LIMIT, W_FRAC, MAX_REQUESTS, RETRY_PAUSES = 1200, D('0.70'), 2000, (60, 120)
W2 = {'l2Book', 'allMids', 'clearinghouseState', 'orderStatus', 'spotClearinghouseState', 'exchangeStatus'}
PER20 = {'recentTrades', 'historicalOrders', 'userFills', 'userFillsByTime', 'fundingHistory', 'userFunding',
         'nonUserFundingUpdates', 'twapHistory', 'userTwapSliceFills', 'userTwapSliceFillsByTime',
         'delegatorHistory', 'delegatorRewards', 'validatorStats'}
TAKER_FEE = D('0.00045')          # perps tier 0 (base rate) taker, paid on every mirrored fill
MIN_ORDER_USD = D('10')
DAY_MS = 86400000

# ---- protocol: frozen to raw/protocol_frozen.json before the bulk fetch -------------------------------
PROTOCOL = {
    'version': 'copytrade_hl_20260925/v1',
    'universe': {
        'ranking': 'point-in-time (PIT) all-time PnL = leaderboard allTime.pnl - month.pnl, descending, ties by address; '
                   'a leaderboard row = master + its sub-accounts',
        'top_n': 200, 'control_n': 200,
        'control_positions': 'floor((i+0.5)*N/200) for i=0..199, 0-based positions in the PIT ranking of all N rows',
        'sub_accounts': 'subAccounts request per candidate; trader = master + all subs; traders with more than K subs '
                        'excluded; K = largest k<=10 whose portfolio requests fit the phase-1 cap',
        'phase1_request_cap': 1400, 'max_subs': 10, 'order': 'interleaved top/control by rank'},
    'blocks': {
        'n_blocks': 4, 'block_days': 30,
        'anchor': 'per account: last sample of the window (= fetch time)',
        'boundary': 'nearest history sample to anchor-30*k days (ties: earlier); boundary before the first sample -> '
                    'first sample (inception, pnl 0); nearest sample farther than 7 days -> account-block invalid',
        'max_boundary_offset_days': 7, 'windows_primary': 'perpAllTime', 'windows_secondary': 'allTime'},
    'roi': {'definition': 'sum over accounts of dPnL / sum over accounts of time-weighted mean account value over '
                          'the block (trapezoid on samples); an account created after the block end contributes 0',
            'min_capital_usd': 1000, 'active': 'dPnL != 0'},
    'primary_test': {
        'formation_block': 3, 'evaluation_block': 4, 'sample': 'top200 U control200, valid ROI in both blocks',
        'deciles': 'sort by ROI ascending (ties by address), decile = floor(10*i/n); decile 9 = top',
        'stats': ['spearman_rho', 'top_decile_share_positive_minus_base', 'top_decile_mean_roi_minus_all',
                  'decile_transition_10x10'],
        'bootstrap': {'draws': 2000, 'seed': 20260925, 'unit': 'trader', 'ci': 'percentile 2.5/97.5'},
        'min_n': 50,
        'gate': 'spearman CI low > 0 AND top-decile share-positive excess CI low > 0',
        'if_gate_fails': 'stop: no followability step (brief: verdict NO-GO for following)'},
    'secondary': ['per group (top / control)', 'absolute PnL instead of ROI', 'total windows (allTime: perp+spot)',
                  'earlier pairs 1->2 and 2->3, mean rho over the three pairs',
                  'leaderboard full population: PIT rank -> next-month pnl sign and leaderboard roi'],
    'followability': {
        'followed': 'top decile (primary ROI) in block 3 AND top decile in block 2; if fewer than 3: top decile in '
                    'block 3 with ROI_2 > 0; cap 20 by ROI_3',
        'window': 'block 4 (last 30 days); userFillsByTime pages of 2000, <=5 pages per account; only accounts with '
                  'perp dPnL != 0 in block 4', 'delays_s': [0, 10, 60, 300, 1800],
        'price': 'close of the candle containing t+d; finest interval available: 1m (last 5000 min), else 5m, else 15m',
        'fee': 'perps tier-0 taker 0.045% on every mirrored fill', 'funding': 'leader userFunding per unit position',
        'episodes': 'per account+coin, flat->flat (a flip closes and reopens); left/right-censored excluded',
        'metric': 'sum follower PnL / sum entry notional; cluster bootstrap by trader'},
    'scale': {'capital_usd': [500, 5000], 'min_order_usd': 10},
}
SEED, DRAWS = PROTOCOL['primary_test']['bootstrap']['seed'], PROTOCOL['primary_test']['bootstrap']['draws']


def jdump(o): return json.dumps(o, sort_keys=True, separators=(',', ':'))


def rnd(x, n=6):
    if x is None: return None
    return round(float(x), n)


def usd(x): return None if x is None else str(D(x).quantize(D('0.01')))


def weight(body, n_items):
    t = (body or {}).get('type')
    w = 2 if t in W2 else 60 if t == 'userRole' else 20
    if t in PER20: w += math.ceil(n_items / 20)
    if t == 'candleSnapshot': w += math.ceil(n_items / 60)
    return w


class Abort(Exception): pass


class Store:
    """raw/<rel>.json.gz = verbatim response bytes (gzip, mtime 0); raw/<rel>.meta.json = request, attempts, sha256.
    Both write-once ('x' mode). Online: missing keys are fetched sequentially with weight pacing."""

    def __init__(self, root, online=False):
        self.root, self.online, self.new = root, online, 0
        self.used = sum(len(self.meta(p).get('attempts', [])) for p in self.metas()) if online else 0

    def metas(self):
        out = []
        for dp, _, fs in os.walk(self.root):
            out += [os.path.join(dp, f) for f in fs if f.endswith('.meta.json')]
        return sorted(out)

    @staticmethod
    def meta(p):
        with open(p) as f: return json.load(f)

    def paths(self, rel): return os.path.join(self.root, rel + '.json.gz'), os.path.join(self.root, rel + '.meta.json')

    def load(self, rel, text=False):
        gz, meta = self.paths(rel)
        if not os.path.exists(meta): return None, None
        m = self.meta(meta)
        if not os.path.exists(gz): return m, None
        with gzip.open(gz) as f: raw = f.read()
        if hashlib.sha256(raw).hexdigest() != m['sha256']: raise ValueError('sha256 mismatch ' + rel)
        return m, (raw.decode('utf-8', 'replace') if text else json.loads(raw))

    def get(self, rel, url, body=None, text=False):
        m, j = self.load(rel, text)
        if m is not None or not self.online: return j
        gz, meta = self.paths(rel)
        os.makedirs(os.path.dirname(gz), exist_ok=True)
        data = json.dumps(body, separators=(',', ':')).encode() if body is not None else None
        attempts, raw = [], None
        for k in range(3):
            if self.used >= MAX_REQUESTS: raise Abort('request cap %d reached' % MAX_REQUESTS)
            self.used += 1; self.new += 1
            t0 = int(time.time() * 1000)
            req = urllib.request.Request(url, data=data, method='POST' if data else 'GET', headers={
                'Content-Type': 'application/json', 'User-Agent': UA, 'Accept-Encoding': 'identity'})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    raw = r.read()
                    hdr = {h: r.headers.get(h) for h in ('Date', 'Last-Modified') if r.headers.get(h)}
                attempts.append({'t_ms': t0, 'dt_ms': int(time.time() * 1000) - t0, 'status': r.status, 'headers': hdr})
                break
            except urllib.error.HTTPError as e:
                attempts.append({'t_ms': t0, 'status': e.code, 'body': e.read()[:300].decode('utf-8', 'replace')})
            except Exception as e:
                attempts.append({'t_ms': t0, 'status': None, 'error': repr(e)[:300]})
            if k < 2: time.sleep(RETRY_PAUSES[k])
        n = 0
        if raw is not None:
            if not text:
                j = json.loads(raw)
                n = len(j) if isinstance(j, list) else 0
            else:
                j = raw.decode('utf-8', 'replace')
            with open(gz, 'xb') as f: f.write(gzip.compress(raw, 9, mtime=0))
        m = {'url': url, 'body': body, 'attempts': attempts, 'n_items': n, 'weight': weight(body, n),
             'sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None, 'bytes': len(raw or b'')}
        with open(meta, 'x') as f: json.dump(m, f, indent=1, sort_keys=True)
        time.sleep(float(D(m['weight']) * 60 / (W_LIMIT * W_FRAC)))
        if raw is None: raise Abort('3 failed attempts: %s %s' % (url, jdump(body)))
        return j


# ---- universe --------------------------------------------------------------------------------------
def lb_rows(store):
    _, j = store.load('leaderboard/leaderboard')
    out = []
    for r in j['leaderboardRows']:
        w = {k: v for k, v in r['windowPerformances']}
        out.append({'addr': r['ethAddress'].lower(), 'av': D(r['accountValue']),
                    'pnl_all': D(w['allTime']['pnl']), 'pnl_month': D(w['month']['pnl']), 'roi_month': D(w['month']['roi']),
                    'vlm_all': D(w['allTime']['vlm']), 'vlm_month': D(w['month']['vlm'])})
    return out


def pit_ranking(rows):
    return sorted(rows, key=lambda r: (-(r['pnl_all'] - r['pnl_month']), r['addr']))


def candidates(ranked, top_n=200, ctrl_n=200):
    """-> (top addrs, control addrs, interleaved unique order)."""
    n = len(ranked)
    top = [r['addr'] for r in ranked[:top_n]]
    ctrl = [ranked[(2 * i + 1) * n // (2 * ctrl_n)]['addr'] for i in range(ctrl_n)]
    order, seen = [], set()
    for pair in zip(top, ctrl):
        for a in pair:
            if a not in seen: seen.add(a); order.append(a)
    return top, ctrl, order


def sub_list(j):
    return sorted(s['subAccountUser'].lower() for s in (j or []))


def choose_k(nsubs, budget, kmax=10):
    """Largest k<=kmax such that portfolio requests (master + subs for traders with <=k subs) fit the budget."""
    best = None
    for k in range(kmax + 1):
        if sum(1 + s for s in nsubs if s <= k) <= budget: best = k
    return best


def universe(store):
    rows = lb_rows(store)
    ranked = pit_ranking(rows)
    top, ctrl, order = candidates(ranked, PROTOCOL['universe']['top_n'], PROTOCOL['universe']['control_n'])
    subs = {}
    for a in order:
        _, j = store.load('subaccounts/' + a)
        subs[a] = sub_list(j) if _ is not None else None
    k = None
    if all(v is not None for v in subs.values()):
        k = choose_k([len(subs[a]) for a in order], PROTOCOL['universe']['phase1_request_cap'] - len(order),
                     PROTOCOL['universe']['max_subs'])
    return {'rows': rows, 'ranked': ranked, 'top': top, 'ctrl': ctrl, 'order': order, 'subs': subs, 'K': k}


def fetch_phase1(store, log):
    u = universe(store)
    for i, a in enumerate(u['order']):
        store.get('subaccounts/' + a, API, {'type': 'subAccounts', 'user': a})
        if i % 50 == 0: log('subAccounts %d/%d used=%d' % (i, len(u['order']), store.used))
    u = universe(store)
    log('K=%s' % u['K'])
    for i, a in enumerate(u['order']):
        if len(u['subs'][a]) > u['K']: continue
        for acct in [a] + u['subs'][a]:
            store.get('portfolio/' + acct, API, {'type': 'portfolio', 'user': acct})
        if i % 25 == 0: log('portfolio trader %d/%d used=%d' % (i, len(u['order']), store.used))


# ---- portfolio history -> 30-day blocks -> ROI ---------------------------------------------------------
def series(pf, window):
    """Portfolio window -> (ts, pnl, av) (Decimal values, common timestamps); None if the window is absent."""
    if pf is None: return None
    d = {k: v for k, v in pf}.get(window)
    if d is None: return None
    pn = {int(t): D(x) for t, x in d.get('pnlHistory') or []}
    av = {int(t): D(x) for t, x in d.get('accountValueHistory') or []}
    ts = sorted(set(pn) & set(av))
    return ts, [pn[t] for t in ts], [av[t] for t in ts]


def boundary(ts, av, b, tol):
    """Index of the history sample used for nominal boundary b (nearest, ties earlier); None if unusable."""
    if b < ts[0]: return 0 if (av[0] == 0 or ts[0] - b <= tol) else None
    i = bisect.bisect_left(ts, b)
    if i == len(ts): j = i - 1
    elif ts[i] == b: j = i
    else: j = i if ts[i] - b < b - ts[i - 1] else i - 1
    return j if abs(ts[j] - b) <= tol else None


def acct_block(s, k, nb=4, bd=30, tol_days=7):
    """Block k (1..nb, nb = most recent) of one account -> ('ok'|'absent', dpnl, capital) or ('invalid', reason)."""
    ts, pn, av = s
    if not ts: return ('absent', D(0), D(0))
    T, tol = ts[-1], tol_days * DAY_MS
    b0, b1 = T - (nb - k + 1) * bd * DAY_MS, T - (nb - k) * bd * DAY_MS
    if b1 < ts[0] and av[0] == 0: return ('absent', D(0), D(0))
    i0, i1 = boundary(ts, av, b0, tol), boundary(ts, av, b1, tol)
    if i0 is None or i1 is None: return ('invalid', 'sparse_or_truncated')
    if i1 <= i0: return ('invalid', 'degenerate')
    area = sum((av[i] + av[i + 1]) * (ts[i + 1] - ts[i]) for i in range(i0, i1))
    return ('ok', pn[i1] - pn[i0], area / 2 / (ts[i1] - ts[i0]))


def trader_block(slist, k, min_cap=D(1000)):
    """Aggregate master + subs -> ({'dp','cap','roi'}, 'ok') or (None, reason)."""
    dp = cap = D(0)
    for s in slist:
        if s is None: return None, 'missing'
        r = acct_block(s, k)
        if r[0] == 'invalid': return None, r[1]
        dp += r[1]; cap += r[2]
    if cap < min_cap: return None, 'capital<min' + ('_active' if abs(dp) > 100 else '')
    if dp == 0: return None, 'inactive'
    return {'dp': dp, 'cap': cap, 'roi': float(dp / cap)}, 'ok'


def build_traders(store, u):
    """-> {addr: {'groups','subs','blocks':{'perp'|'total': {k: dict|None}},'why':{...},'end_av','month_dp'}}"""
    top, ctrl = set(u['top']), set(u['ctrl'])
    out = {}
    for a in u['order']:
        t = {'groups': sorted(g for g, s in (('top', top), ('ctrl', ctrl)) if a in s), 'subs': u['subs'][a]}
        out[a] = t
        if t['subs'] is None: t['excluded'] = 'no_subaccounts_response'; continue
        if u['K'] is None or len(t['subs']) > u['K']: t['excluded'] = 'subs>K'; continue
        pfs = [store.load('portfolio/' + x)[1] for x in [a] + t['subs']]
        if any(p is None for p in pfs): t['excluded'] = 'portfolio_missing'; continue
        t['excluded'] = None
        t['blocks'], t['why'] = {}, {}
        for wt, win in (('perp', 'perpAllTime'), ('total', 'allTime')):
            sl = [series(p, win) for p in pfs]
            t['blocks'][wt], t['why'][wt] = {}, {}
            for k in range(1, 5):
                t['blocks'][wt][k], t['why'][wt][k] = trader_block(sl, k)
        tot = [series(p, 'allTime') for p in pfs]
        mon = [series(p, 'month') for p in pfs]
        t['end_av'] = sum((s[2][-1] for s in tot if s and s[0]), D(0))
        t['end_perp_av'] = sum((s[2][-1] for s in (series(p, 'perpAllTime') for p in pfs) if s and s[0]), D(0))
        t['month_dp'] = sum((s[1][-1] - s[1][0] for s in mon if s and s[0]), D(0))
        t['anchor_ms'] = max((s[0][-1] for s in tot if s and s[0]), default=None)
    return out


# ---- statistics ---------------------------------------------------------------------------------------
def avg_ranks(x):
    idx = sorted(range(len(x)), key=lambda i: x[i])
    r, i = [0.0] * len(x), 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and x[idx[j + 1]] == x[idx[i]]: j += 1
        for q in range(i, j + 1): r[idx[q]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a, b):
    n = len(a)
    if n < 3: return None
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    saa, sbb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return None if saa == 0 or sbb == 0 else sab / math.sqrt(saa * sbb)


def spearman(x, y): return pearson(avg_ranks(x), avg_ranks(y))


def deciles(x, keys):
    """Decile 0..9 per item: sort ascending by (value, key), decile = floor(10*pos/n)."""
    n = len(x)
    order = sorted(range(n), key=lambda i: (x[i], keys[i]))
    d = [0] * n
    for p, i in enumerate(order): d[i] = 10 * p // n
    return d


def transition(xa, xb, keys):
    da, db = deciles(xa, keys), deciles(xb, keys)
    m = [[0] * 10 for _ in range(10)]
    for i, j in zip(da, db): m[i][j] += 1
    return m


def pair_stats(xa, xb, keys):
    n = len(xa)
    da = deciles(xa, keys)
    top = [i for i in range(n) if da[i] == 9]
    base = sum(1 for v in xb if v > 0) / n
    stop = sum(1 for i in top if xb[i] > 0) / len(top)
    mall, mtop = sum(xb) / n, sum(xb[i] for i in top) / len(top)
    return {'rho': spearman(xa, xb), 'share_pos_top': stop, 'base_pos': base, 'excess': stop - base,
            'mean_b_top': mtop, 'mean_b_all': mall, 'mean_diff': mtop - mall, 'n_top': len(top),
            'k_pos_top': sum(1 for i in top if xb[i] > 0), 'k_pos_all': sum(1 for v in xb if v > 0)}


def pctl(v, q):
    v = sorted(x for x in v if x is not None)
    if not v: return None
    h = (len(v) - 1) * q
    lo, hi = math.floor(h), math.ceil(h)
    return v[lo] + (v[hi] - v[lo]) * (h - lo)


def draws_idx(n, draws=DRAWS, seed=SEED):
    rng = random.Random(seed)
    for _ in range(draws): yield [int(rng.random() * n) for _ in range(n)]


def ci(vals):
    v = [x for x in vals if x is not None]
    return {'lo': rnd(pctl(v, 0.025)), 'hi': rnd(pctl(v, 0.975)), 'share_draws_le_0': rnd(sum(1 for x in v if x <= 0) / len(v)) if v else None}


def pair_report(items, draws=DRAWS, seed=SEED, with_matrix=True, with_means=True):
    """items: [(key, xa, xb)] -> point stats, bootstrap CIs (resampling traders), decile transition matrix.
    Means are reported only as the top-decile-minus-all difference (with_means=False drops it, e.g. for dollars)."""
    n = len(items)
    if n < 20: return {'n': n, 'note': 'too few traders'}
    keys, xa, xb = [i[0] for i in items], [i[1] for i in items], [i[2] for i in items]
    pt = pair_stats(xa, xb, keys)
    bs = {k: [] for k in ('rho', 'excess', 'mean_diff')}
    for idx in draws_idx(n, draws, seed):
        s = pair_stats([xa[i] for i in idx], [xb[i] for i in idx], [(keys[i], j) for j, i in enumerate(idx)])
        for k in bs: bs[k].append(s[k])
    out = {'n': n, 'n_top_decile': pt['n_top'], 'spearman_rho': rnd(pt['rho']), 'spearman_ci': ci(bs['rho']),
           'top_decile_positive_B': '%d/%d' % (pt['k_pos_top'], pt['n_top']), 'all_positive_B': '%d/%d' % (pt['k_pos_all'], n),
           'top_decile_share_positive_B': rnd(pt['share_pos_top']), 'base_share_positive_B': rnd(pt['base_pos']),
           'excess_share_positive': rnd(pt['excess']), 'excess_ci': ci(bs['excess'])}
    if with_means:
        out.update({'mean_B_diff': rnd(pt['mean_diff']), 'mean_B_diff_ci': ci(bs['mean_diff'])})
    if with_matrix:
        m = transition(xa, xb, keys)
        out['decile_transition'] = m
        out['top_to_top_share'] = rnd(m[9][9] / sum(m[9])) if sum(m[9]) else None
        out['diagonal_share'] = rnd(sum(m[i][i] for i in range(10)) / n)
    return out


def sample(traders, wt, ka, kb, metric='roi', group=None):
    out = []
    for a in sorted(traders):
        t = traders[a]
        if t.get('excluded') or (group and group not in t['groups']): continue
        A, B = t['blocks'][wt][ka], t['blocks'][wt][kb]
        if A is None or B is None: continue
        out.append((a, A[metric] if metric == 'roi' else float(A['dp']), B[metric] if metric == 'roi' else float(B['dp'])))
    return out


def mean_rho_pairs(traders, wt='perp', draws=DRAWS, seed=SEED):
    """Mean Spearman rho over pairs (1,2),(2,3),(3,4); bootstrap resamples traders once per draw."""
    keys = [a for a in sorted(traders) if not traders[a].get('excluded')]
    pairs = [(1, 2), (2, 3), (3, 4)]
    def val(ix):
        rs = []
        for ka, kb in pairs:
            v = [(traders[keys[i]]['blocks'][wt][ka], traders[keys[i]]['blocks'][wt][kb]) for i in ix]
            v = [(p['roi'], q['roi']) for p, q in v if p and q]
            r = spearman([x for x, _ in v], [y for _, y in v]) if len(v) >= 20 else None
            rs.append(r)
        return rs
    pt = val(range(len(keys)))
    bs = []
    for idx in draws_idx(len(keys), draws, seed):
        rs = val(idx)
        bs.append(sum(rs) / 3 if all(r is not None for r in rs) else None)
    return {'rho_by_pair': {'%d->%d' % p: rnd(r) for p, r in zip(pairs, pt)},
            'mean_rho': rnd(sum(pt) / 3) if all(r is not None for r in pt) else None, 'mean_rho_ci': ci(bs),
            'note': 'pairs 1->2 and 2->3 share the universe selection (PIT at block-4 start) with their B block: '
                    'mild look-ahead, reported as secondary only'}


def lb_population(u, draws=DRAWS, seed=SEED):
    """Full leaderboard: PIT rank (all-time PnL before the month) -> the month that followed (leaderboard windows)."""
    rows, top = u['rows'], set(u['top'])
    act = [r for r in u['ranked'] if r['vlm_month'] > 0 or r['pnl_month'] != 0]
    def summ(rs):
        n = len(rs)
        return {'n': n, 'share_pnl_pos': rnd(sum(1 for r in rs if r['pnl_month'] > 0) / n),
                'share_pnl_neg': rnd(sum(1 for r in rs if r['pnl_month'] < 0) / n)}
    trows = [r for r in u['ranked'] if r['addr'] in top]
    tact = [r for r in trows if r['vlm_month'] > 0 or r['pnl_month'] != 0]
    base = sum(1 for r in act if r['pnl_month'] > 0) / len(act)
    ex = []
    for idx in draws_idx(len(tact), draws, seed):
        ex.append(sum(1 for i in idx if tact[i]['pnl_month'] > 0) / len(idx) - base)
    # deciles of PIT rank among month-active rows: share positive next month
    n = len(act)
    dec = [[0, 0] for _ in range(10)]
    for p, r in enumerate(act):  # act is in PIT order (best first) -> decile 0 = best
        d = 10 * p // n
        dec[d][0] += 1; dec[d][1] += r['pnl_month'] > 0
    rk = avg_ranks([-float(r['pnl_all'] - r['pnl_month']) for r in act])
    rm = avg_ranks([float(r['pnl_month']) for r in act])
    rho = pearson([-x for x in rk], rm)
    bs = []
    for idx in draws_idx(n, draws, seed):
        bs.append(pearson([-rk[i] for i in idx], [rm[i] for i in idx]))
    return {'what': 'all leaderboard rows; formation = all-time PnL before the last 30 days (allTime-month), '
                    'evaluation = leaderboard month window; sub-accounts aggregated by the leaderboard',
            'all_rows': summ(rows), 'month_active_rows': summ(act), 'pit_top200_all': summ(trows),
            'pit_top200_month_active': summ(tact),
            'pit_top200_active_excess_share_pos_vs_active_base': rnd(sum(1 for r in tact if r['pnl_month'] > 0) / len(tact) - base),
            'excess_ci': ci(ex),
            'share_pos_by_pit_decile_active_best_first': [rnd(c / m) for m, c in dec],
            'spearman_pit_pnl_vs_month_pnl_active': rnd(rho),
            'spearman_ci': ci(bs), 'spearman_bootstrap_note': 'ranks computed once on the full sample, resampled pairs',
            'note': 'absolute PnL ranks, size-driven'}


def primary(traders):
    """The pre-registered primary test and its gate: perp ROI, formation block 3 -> evaluation block 4."""
    P = PROTOCOL['primary_test']
    prim = pair_report(sample(traders, 'perp', P['formation_block'], P['evaluation_block']))
    rho_ok = prim.get('spearman_ci', {}).get('lo') is not None and prim['spearman_ci']['lo'] > 0
    ex_ok = prim.get('excess_ci', {}).get('lo') is not None and prim['excess_ci']['lo'] > 0
    gate = {'n': prim['n'], 'enough_n': prim['n'] >= P['min_n'], 'rho_ci_lo_gt_0': rho_ok, 'excess_ci_lo_gt_0': ex_ok}
    gate['pass'] = bool(gate['enough_n'] and rho_ok and ex_ok)
    return prim, gate


def persistence(traders, u):
    P = PROTOCOL['primary_test']
    ka, kb = P['formation_block'], P['evaluation_block']
    prim, gate = primary(traders)
    sec = {
        'top_group_perp_roi_3to4': pair_report(sample(traders, 'perp', ka, kb, group='top'), with_matrix=False),
        'control_group_perp_roi_3to4': pair_report(sample(traders, 'perp', ka, kb, group='ctrl'), with_matrix=False),
        'abs_pnl_perp_3to4': pair_report(sample(traders, 'perp', ka, kb, metric='dp'), with_matrix=False, with_means=False),
        'total_roi_3to4': pair_report(sample(traders, 'total', ka, kb), with_matrix=False),
        'perp_roi_1to2': pair_report(sample(traders, 'perp', 1, 2), with_matrix=False),
        'perp_roi_2to3': pair_report(sample(traders, 'perp', 2, 3), with_matrix=False),
        'mean_rho_three_pairs_perp': mean_rho_pairs(traders),
        'leaderboard_population': lb_population(u),
    }
    diag = {'note': 'NOT pre-registered: looked at after the primary result, to check whether the 2->3 sign flip is '
                    'a top-group selection (collider) artifact; the control group cannot carry that bias'}
    for ka_, kb_ in ((1, 2), (2, 3), (3, 4)):
        for g in ('top', 'ctrl'):
            it = sample(traders, 'perp', ka_, kb_, group=g)
            diag['%d->%d_%s' % (ka_, kb_, g)] = {'n': len(it), 'rho': rnd(spearman([i[1] for i in it], [i[2] for i in it]))}
    sec['diagnostic_rho_by_pair_and_group'] = diag
    return prim, gate, sec


# ---- fills -> positions -> delayed follower -------------------------------------------------------------
def is_perp(f): return not f['coin'].startswith('@') and '/' not in f['coin'] and f.get('dir') not in ('Buy', 'Sell')


def episodes(fills):
    """Per coin, time order (time, tid): flat->flat position episodes. A flip closes and reopens at the same fill.
    -> list of {'coin','side':+1/-1,'legs':[(t, dsz, px)],'t_open','t_close','closed','left_censored','leader_fee'}"""
    out, by = [], {}
    for f in fills:
        if is_perp(f): by.setdefault(f['coin'], []).append(f)
    for coin in sorted(by):
        fs = sorted(by[coin], key=lambda f: (int(f['time']), int(f.get('tid', 0))))
        pos, cur = None, None
        for f in fs:
            t, px, sz = int(f['time']), D(f['px']), D(f['sz'])
            dsz = sz if f['side'] == 'B' else -sz
            sp = D(f['startPosition'])
            if pos is None or sp != pos:            # first fill or a gap in the record: restart tracking
                if cur is not None: cur['broken'] = True; out.append(cur)
                cur = None
                if sp != 0:
                    cur = {'coin': coin, 'side': 1 if sp > 0 else -1, 'legs': [], 't_open': t, 'left_censored': True,
                           'closed': False, 'leader_fee': D(0)}
                pos = sp
            new = pos + dsz
            fee = D(f.get('fee', '0'))
            if pos == 0:
                cur = {'coin': coin, 'side': 1 if dsz > 0 else -1, 'legs': [(t, dsz, px)], 't_open': t,
                       'left_censored': False, 'closed': False, 'leader_fee': fee}
            elif new == 0 or (new > 0) == (pos > 0):
                cur['legs'].append((t, dsz, px)); cur['leader_fee'] += fee
            else:                                   # flip: close -pos, open new
                cur['legs'].append((t, -pos, px)); cur['leader_fee'] += fee * (abs(pos) / sz)
                cur['closed'], cur['t_close'] = True, t
                out.append(cur)
                cur = {'coin': coin, 'side': 1 if new > 0 else -1, 'legs': [(t, new, px)], 't_open': t,
                       'left_censored': False, 'closed': False, 'leader_fee': fee * (abs(new) / sz)}
            if new == 0 and cur is not None:
                cur['closed'], cur['t_close'] = True, t
                out.append(cur); cur = None
            pos = new
        if cur is not None: out.append(cur)
    return out


class Prices:
    """Candle closes by coin and interval; price(coin, t) = close of the finest candle containing t."""
    IV = (('1m', 60000), ('5m', 300000), ('15m', 900000))

    def __init__(self): self.c = {}

    def add(self, coin, iv, candles):
        d = self.c.setdefault((coin, iv), {})
        for k in candles: d[int(k['t'])] = D(k['c'])

    def price(self, coin, t):
        for iv, ms in self.IV:
            d = self.c.get((coin, iv))
            if d:
                o = t - t % ms
                if o in d: return d[o], iv
        return None, None


def follow_episode(ep, prices, d_ms, fee=TAKER_FEE):
    """Mirror every leg at t+d at the candle close -> (net pnl, entry notional, fees, interval set) or None."""
    pnl = notional = fees = D(0)
    pos, ivs = D(0), set()
    for t, dsz, _ in ep['legs']:
        p, iv = prices.price(ep['coin'], t + d_ms)
        if p is None: return None
        ivs.add(iv)
        v = abs(dsz) * p
        if pos == 0 or (pos > 0) == (dsz > 0): notional += v
        pnl -= dsz * p
        fees += v * fee
        pos += dsz
    return pnl - fees, notional, fees, ivs


def leader_episode(ep, fee=TAKER_FEE):
    pnl = notional = fees = D(0)
    pos = D(0)
    for _, dsz, px in ep['legs']:
        v = abs(dsz) * px
        if pos == 0 or (pos > 0) == (dsz > 0): notional += v
        pnl -= dsz * px; fees += v * fee; pos += dsz
    return pnl - fees, notional, fees


# ---- report ------------------------------------------------------------------------------------------
def doc_text(s):
    t = html.unescape(re.sub(r'<[^>]+>', ' ', s))
    t = re.sub(r'\s+', ' ', t)
    return re.sub(r' ([,.:;])', r'\1', t)


def docs_check(store):
    out = {}
    for name, (url, quotes) in sorted(DOC_PAGES.items()):
        m, txt = store.load('docs/' + name, text=True)
        if m is None: out[name] = {'url': url, 'saved': False}; continue
        t = doc_text(txt)
        out[name] = {'url': url, 'saved': True, 'sha256': m['sha256'], 'fetched_ms': m['attempts'][-1]['t_ms'],
                     'quotes': {q: q in t for q in quotes}}
    return out


def request_stats(store, frozen_ms):
    per, n_att, w, s429, fail, ts, expl = {}, 0, 0, 0, 0, [], 0
    for p in store.metas():
        m = store.meta(p)
        if m['url'].startswith(GB): continue
        typ = (m.get('body') or {}).get('type', 'leaderboard')
        att = m.get('attempts', [])
        per[typ] = per.get(typ, 0) + len(att)
        n_att += len(att); w += m.get('weight', 0)
        s429 += sum(1 for x in att if x.get('status') == 429)
        fail += m.get('sha256') is None
        ts += [x['t_ms'] for x in att]
        expl += sum(1 for x in att if x['t_ms'] < frozen_ms)
    return {'api_requests_total': n_att, 'by_type': per, 'exploratory_before_protocol_freeze': expl,
            'weight_total': w, 'http_429': s429, 'failed_keys': fail,
            'first_utc_ms': min(ts) if ts else None, 'last_utc_ms': max(ts) if ts else None,
            'span_min': rnd((max(ts) - min(ts)) / 60000, 1) if ts else None,
            'pacing': 'sleep weight*60/(0.70*1200) s after each request (1.43 s per weight-20 request); '
                      '429/errors: 60 s then 120 s, max 3 attempts'}


def universe_report(u, traders):
    ex = {}
    for a, t in traders.items():
        for g in t['groups']: ex.setdefault(g, {}).setdefault(str(t.get('excluded')), 0); ex[g][str(t.get('excluded'))] += 1
    ns = [len(v) for v in u['subs'].values() if v is not None]
    rows = u['rows']
    cur = {r['addr'] for r in sorted(rows, key=lambda r: (-r['pnl_all'], r['addr']))[:200]}
    tops = [r for r in u['ranked'][:200]]
    return {'leaderboard_rows': len(rows), 'top_n': len(u['top']), 'control_n': len(u['ctrl']),
            'union': len(u['order']), 'top_control_overlap': len(set(u['top']) & set(u['ctrl'])),
            'current_top200_overlap_with_pit_top200': len(cur & set(u['top'])),
            'K_max_subs': u['K'], 'subs_count_hist': {str(k): ns.count(k) for k in sorted(set(ns))},
            'status_by_group': ex,
            'survivorship': {'rows_av_zero': sum(1 for r in rows if r['av'] == 0),
                             'rows_av_lt_100': sum(1 for r in rows if r['av'] < 100),
                             'rows_month_active': sum(1 for r in rows if r['vlm_month'] > 0 or r['pnl_month'] != 0),
                             'pit_top200_now_av_lt_1000': sum(1 for r in tops if r['av'] < 1000),
                             'note': 'the list keeps zero-balance rows, but its inclusion rule is undocumented; '
                                     'accounts absent from the file cannot be sampled'}}


def data_quality(traders, u):
    """Does master + subs reproduce the leaderboard row? (portfolio fetched ~40-75 min after the leaderboard file)"""
    lb = {r['addr']: r for r in u['rows']}
    why, agg = {}, {}
    for a, t in traders.items():
        if t.get('excluded'): continue
        r = lb[a]
        g = agg.setdefault('with_subs' if t['subs'] else 'no_subs', {'n': 0, 'av': [], 'pnl': []})
        g['n'] += 1
        g['av'].append(float(abs(t['end_av'] - r['av']) / max(r['av'], D(1000))))
        g['pnl'].append(float(abs(t['month_dp'] - r['pnl_month']) / max(abs(r['pnl_month']), D(1000))))
        for k in (3, 4):
            w = t['why']['perp'][k]; why[w] = why.get(w, 0) + 1
    rep = {}
    for g, v in sorted(agg.items()):
        rep[g] = {'n': v['n'], 'share_av_within_5pct': rnd(sum(1 for x in v['av'] if x <= .05) / v['n']),
                  'share_month_pnl_within_5pct': rnd(sum(1 for x in v['pnl'] if x <= .05) / v['n']),
                  'month_pnl_rel_diff_q50_q90': [rnd(pctl(v['pnl'], .5), 4), rnd(pctl(v['pnl'], .9), 4)]}
    return {'traders_fetched': sum(v['n'] for v in agg.values()), 'leaderboard_vs_master_plus_subs': rep,
            'perp_block_status_counts_blocks_3_4': why,
            'note': 'relative diff = |portfolio - leaderboard| / max(|leaderboard|, $1000); residual gaps come from the '
                    'time shift, equally with and without sub-accounts'}


def followed_traders(traders):
    """Pre-registered: top decile (perp ROI) in block 2 AND block 3, deciles among traders valid in both."""
    items = sample(traders, 'perp', 2, 3)
    if len(items) < 20: return []
    keys, x2, x3 = [i[0] for i in items], [i[1] for i in items], [i[2] for i in items]
    d2, d3 = deciles(x2, keys), deciles(x3, keys)
    sel = [(x3[i], keys[i]) for i in range(len(keys)) if d2[i] == 9 and d3[i] == 9]
    if len(sel) < 3: sel = [(x3[i], keys[i]) for i in range(len(keys)) if d3[i] == 9 and x2[i] > 0]
    return [a for _, a in sorted(sel, key=lambda z: (-z[0], z[1]))[:20]]


def paged(store, kind, acct, s, e, per_page, max_pages=5):
    """Time-paginated userFillsByTime / userFunding; next startTime = last returned time, duplicates dropped."""
    out, seen, start = [], set(), s
    for _ in range(max_pages):
        body = {'type': kind, 'user': acct, 'startTime': start, 'endTime': e}
        if kind == 'userFillsByTime': body['aggregateByTime'] = False
        j = store.get('%s/%s/%d' % (kind, acct, start), API, body)
        if not j: break
        for x in j:
            k = (x.get('tid'), x.get('oid'), x.get('side'), x['time'], x.get('coin') or x.get('delta', {}).get('coin'))
            if k not in seen: seen.add(k); out.append(x)
        if len(j) < per_page: break
        nxt = max(int(x['time']) for x in j)
        start = nxt if nxt > start else start + 1
    return out


def safe(coin): return re.sub(r'[^A-Za-z0-9]', '_', coin) + '_' + hashlib.sha1(coin.encode()).hexdigest()[:6]


def load_candles(store, prices, coin, s, e):
    """Per interval: the last 5000 candles up to e (clamped at s), paginated if a response stops early;
    only candles closed before the response time are kept."""
    for iv, ms in Prices.IV:
        start = max(s - s % ms, e - 5000 * ms)
        for _ in range(10):
            rel = 'candles/%s/%s_%d_%d' % (safe(coin), iv, start, e)
            j = store.get(rel, API, {'type': 'candleSnapshot', 'req': {'coin': coin, 'interval': iv, 'startTime': start, 'endTime': e}})
            if not j: break
            m, _ = store.load(rel)
            prices.add(coin, iv, [c for c in j if int(c['T']) < m['attempts'][-1]['t_ms']])
            last = max(int(c['t']) for c in j)
            if last + 2 * ms > e or len(j) < 500: break      # covered, or a short (final) page
            start = last + ms


def holding_stats(eps):
    h = sorted((ep['t_close'] - ep['t_open']) / 1000 for ep in eps)
    if not h: return {'n': 0}
    n = len(h)
    return {'n': n, 'mean_s': rnd(sum(h) / n, 1), 'quantiles_s_10_25_50_75_90': [rnd(pctl(h, q), 1) for q in (.1, .25, .5, .75, .9)],
            'share_lt_1min': rnd(sum(1 for x in h if x < 60) / n), 'share_lt_10min': rnd(sum(1 for x in h if x < 600) / n),
            'share_lt_1h': rnd(sum(1 for x in h if x < 3600) / n), 'share_ge_1d': rnd(sum(1 for x in h if x >= 86400) / n)}


def ratio_boot(per, draws=DRAWS, seed=SEED):
    """per: {trader: (pnl, notional)} -> ratio of sums and trader-cluster bootstrap CI."""
    keys = sorted(per)
    P, N = [per[k][0] for k in keys], [per[k][1] for k in keys]
    r = float(sum(P) / sum(N)) if sum(N) else None
    bs = []
    for idx in draws_idx(len(keys), draws, seed):
        n = sum(N[i] for i in idx)
        bs.append(float(sum(P[i] for i in idx) / n) if n else None)
    return {'pnl_per_unit_notional': rnd(r, 8), 'ci': ci(bs), 'pnl_usd': usd(sum(P)), 'entry_notional_usd': usd(sum(N))}


def followability(store, traders, gate):
    if not gate['pass']:
        return {'skipped': True, 'reason': 'persistence gate failed (pre-registered stop; brief step 2)'}
    F = PROTOCOL['followability']
    fol = followed_traders(traders)
    eps_by, fund_by, cov = {}, {}, {'accounts': 0, 'fills': 0, 'pages_capped': 0}
    for a in fol:
        t = traders[a]
        e = t['anchor_ms']; s = e - 30 * DAY_MS
        eps_by[a], fund_by[a] = [], []
        for acct in [a] + t['subs']:
            sp = series(store.load('portfolio/' + acct)[1], 'perpAllTime')
            r = acct_block(sp, 4) if sp else ('absent',)
            if r[0] != 'ok' or r[1] == 0: continue
            cov['accounts'] += 1
            fills = paged(store, 'userFillsByTime', acct, s, e, 2000)
            cov['fills'] += len(fills)
            cov['pages_capped'] += len(fills) >= 10000
            eps = [ep for ep in episodes(fills) if ep['closed'] and not ep['left_censored'] and not ep.get('broken') and ep['t_open'] >= s]
            for ep in eps: ep['acct'] = acct
            eps_by[a] += eps
            fund_by[a] += [dict(f, acct=acct) for f in paged(store, 'userFunding', acct, s, e, 500)]
    prices = Prices()
    coins = sorted({ep['coin'] for eps in eps_by.values() for ep in eps})
    e_all = max((traders[a]['anchor_ms'] for a in fol), default=0)
    for c in coins: load_candles(store, prices, c, e_all - 30 * DAY_MS, e_all + 2 * 3600000)
    for a, eps in eps_by.items():   # funding of the leader inside each episode, per coin
        for ep in eps:
            ep['funding'] = sum((D(f['delta']['usdc']) for f in fund_by[a] if f['acct'] == ep['acct']
                                 and f['delta'].get('coin') == ep['coin'] and ep['t_open'] <= int(f['time']) <= ep['t_close']), D(0))
    keep = {a: [ep for ep in eps if all(follow_episode(ep, prices, d * 1000) for d in F['delays_s'])] for a, eps in eps_by.items()}
    by_delay = {}
    for d in F['delays_s']:
        per = {}
        for a, eps in keep.items():
            rs = [(follow_episode(ep, prices, d * 1000), ep['funding']) for ep in eps]
            per[a] = (sum((r[0] + fu for r, fu in rs), D(0)), sum((r[1] for r, _ in rs), D(0)))
        by_delay[str(d)] = ratio_boot({k: v for k, v in per.items() if v[1] > 0})
    own = {a: (sum((leader_episode(ep)[0] + ep['funding'] for ep in eps), D(0)), sum((leader_episode(ep)[1] for ep in eps), D(0)))
           for a, eps in keep.items()}
    allk = [ep for eps in keep.values() for ep in eps]
    ivs = {}
    for ep in allk:
        for d in F['delays_s']:
            for iv in follow_episode(ep, prices, d * 1000)[3]: ivs[iv] = ivs.get(iv, 0) + 1
    money, small = {}, {}
    for c in PROTOCOL['scale']['capital_usd']:     # proportional sizing: follower = leader * C / leader block-4 capital
        per, legs, tiny = [], 0, 0
        for a, eps in keep.items():
            b4 = traders[a]['blocks']['perp'][4]
            if not b4: continue
            f = D(c) / b4['cap']
            per.append(sum((follow_episode(ep, prices, 60000)[0] + ep['funding'] for ep in eps), D(0)) * f)
            for ep in eps:
                for _, dsz, px in ep['legs']:
                    legs += 1; tiny += abs(dsz) * px * f < MIN_ORDER_USD
        money[str(c)] = {'traders': len(per), 'mean_usd_30d_delay60s': usd(sum(per, D(0)) / len(per)) if per else None}
        small[str(c)] = {'legs': legs, 'share_below_min_order': rnd(tiny / legs) if legs else None}
    return {'skipped': False, 'followed_traders_n': len(fol), 'coverage': cov,
            'episodes_closed': sum(len(v) for v in eps_by.values()), 'episodes_priced_all_delays': len(allk),
            'holding_time': holding_stats(allk), 'candle_interval_use': ivs,
            'leader_own_price_taker_fee': ratio_boot({k: v for k, v in own.items() if v[1] > 0}),
            'follower_by_delay_s': by_delay, 'funding_included': True,
            'follower_usd_per_30d_by_capital': money, 'orders_below_10usd_by_capital': small}


def scale_report(traders, prim_items):
    out = {}
    tops = []
    if prim_items:
        keys, xa = [i[0] for i in prim_items], [i[1] for i in prim_items]
        d = deciles(xa, keys)
        tops = [keys[i] for i in range(len(keys)) if d[i] == 9]
    avs = sorted(float(traders[a]['end_perp_av']) for a in tops)
    out['top_decile_A_traders'] = len(avs)
    if avs:
        for c in PROTOCOL['scale']['capital_usd']:
            f = [c / v for v in avs if v > 0]
            out['scale_factor_%d' % c] = {'mean': rnd(sum(f) / len(f), 8) if f else None}
    out['min_order_usd'] = str(MIN_ORDER_USD)
    out['note'] = ('from account values, not fills: no position notionals or leverage measured; scale factor = follower '
                   'capital C / current perp account value of a block-3 top-decile trader (proportional sizing)')
    return out


VERDICT_RULE = ('n < min_n -> INSUFFICIENT; gate fails -> NO-GO (brief: stop at step 2); gate passes -> follower at '
                'd=60 s: point <= 0 NO-GO; CI low <= 0 INSUFFICIENT; CI low > 0 and mean 30-day USD at $5000 < $100 '
                'POSITIVE_BUT_TOO_SMALL; else POSITIVE. (Rule written after the protocol freeze, before any result.)')


def verdict(prim, gate, follow):
    if not gate['enough_n']: return {'class': 'INSUFFICIENT', 'why': 'fewer valid traders than min_n', 'rule': VERDICT_RULE}
    if not gate['pass']:
        parts = ['rank correlation %s (rho %s, CI %s..%s)' % ('significant' if gate['rho_ci_lo_gt_0'] else 'not significant',
                 prim['spearman_rho'], prim['spearman_ci']['lo'], prim['spearman_ci']['hi']),
                 'top-decile excess share of positive B %s (%s, CI %s..%s; top decile %s vs all %s)' % (
                 'significant' if gate['excess_ci_lo_gt_0'] else 'not significant', prim['excess_share_positive'],
                 prim['excess_ci']['lo'], prim['excess_ci']['hi'], prim['top_decile_positive_B'], prim['all_positive_B']),
                 'top-decile mean ROI_B minus all: %s (CI %s..%s)' % (prim['mean_B_diff'], prim['mean_B_diff_ci']['lo'],
                                                                     prim['mean_B_diff_ci']['hi'])]
        return {'class': 'NO-GO', 'rule': VERDICT_RULE, 'why': 'persistence gate failed: ' + '; '.join(parts)}
    r = follow['follower_by_delay_s']['60']
    m = follow['follower_usd_per_30d_by_capital']['5000']['mean_usd_30d_delay60s']
    if r['pnl_per_unit_notional'] is None or r['pnl_per_unit_notional'] <= 0: cls = 'NO-GO'
    elif r['ci']['lo'] is None or r['ci']['lo'] <= 0: cls = 'INSUFFICIENT'
    elif m is None or D(m) < 100: cls = 'POSITIVE_BUT_TOO_SMALL'
    else: cls = 'POSITIVE'
    return {'class': cls, 'rule': VERDICT_RULE, 'why': 'follower at 60 s delay after taker fees'}


def build_report(root):
    store = Store(os.path.join(root, 'raw'))
    fz = Store.meta(os.path.join(root, 'raw', 'protocol_frozen.json'))
    u = universe(store)
    traders = build_traders(store, u)
    prim, gate, sec = persistence(traders, u)
    fol = followability(store, traders, gate)
    lbm, _ = store.load('leaderboard/leaderboard')
    rep = {
        'question': 'Do publicly ranked Hyperliquid perp traders keep profitability out of sample, and does a delayed '
                    'follower still make money after fees?',
        'protocol': PROTOCOL, 'protocol_matches_frozen_copy': fz['protocol'] == PROTOCOL,
        'protocol_frozen_utc_ms': fz['frozen_at_ms'],
        'leaderboard': {'url': lbm.get('url', LB_URL), 'sha256': lbm['sha256'], 'bytes': lbm['bytes'],
                        'last_modified': (lbm.get('headers') or lbm['attempts'][-1].get('headers') or {}).get('Last-Modified'),
                        'fetched_utc_ms': lbm['attempts'][-1]['t_ms']},
        'requests': request_stats(store, fz['frozen_at_ms']),
        'docs': docs_check(store), 'fees_used': {'taker': str(TAKER_FEE), 'min_order_usd': str(MIN_ORDER_USD)},
        'universe': universe_report(u, traders), 'data_quality': data_quality(traders, u),
        'persistence_primary': prim, 'gate': gate, 'persistence_secondary': sec,
        'followability': fol, 'scale': scale_report(traders, sample(traders, 'perp', 3, 4)),
        'verdict': verdict(prim, gate, fol)}
    anch = sorted(t['anchor_ms'] for t in traders.values() if not t.get('excluded') and t.get('anchor_ms'))
    iso = lambda ms: time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(ms / 1000))
    rep['blocks_nominal_utc'] = {'anchor_range': [iso(anch[0]), iso(anch[-1])],
                                 'blocks': {str(k): [iso(anch[0] - (5 - k) * 30 * DAY_MS), iso(anch[0] - (4 - k) * 30 * DAY_MS)] for k in range(1, 5)}}
    mr = sec['mean_rho_three_pairs_perp']; lp = sec['leaderboard_population']
    lohi = lambda c: [(c or {}).get('lo'), (c or {}).get('hi')]
    rep['headline'] = {
        'verdict': rep['verdict']['class'], 'persistence_gate_passed': gate['pass'],
        'primary_perp_roi_A3_B4': {'n': prim['n'], 'rho': prim.get('spearman_rho'), 'rho_ci': lohi(prim.get('spearman_ci')),
                                   'top_decile_positive_B': prim.get('top_decile_positive_B'), 'all_positive_B': prim.get('all_positive_B'),
                                   'excess': prim.get('excess_share_positive'), 'excess_ci': lohi(prim.get('excess_ci')),
                                   'mean_roi_B_top_minus_all': prim.get('mean_B_diff'), 'mean_diff_ci': lohi(prim.get('mean_B_diff_ci'))},
        'rho_by_adjacent_pair': mr['rho_by_pair'], 'mean_rho_three_pairs': mr['mean_rho'],
        'mean_rho_ci': [mr['mean_rho_ci']['lo'], mr['mean_rho_ci']['hi']],
        'leaderboard_pit_top200_next_month': {'share_positive': lp['pit_top200_month_active']['share_pnl_pos'],
                                              'active_base': lp['month_active_rows']['share_pnl_pos'],
                                              'excess_ci': [lp['excess_ci']['lo'], lp['excess_ci']['hi']]},
        'follower_simulation': 'not run: gate failed (pre-registered stop)' if fol.get('skipped') else 'see followability',
        'api_requests': rep['requests']['api_requests_total']}
    return rep


def block_network():
    def no(*a, **k): raise RuntimeError('network disabled (--offline)')
    socket.socket.connect = no
    socket.create_connection = no
    socket.getaddrinfo = no


def freeze_protocol(root):
    p = os.path.join(root, 'protocol_frozen.json')
    os.makedirs(root, exist_ok=True)
    if not os.path.exists(p):
        with open(p, 'x') as f: json.dump({'frozen_at_ms': int(time.time() * 1000), 'protocol': PROTOCOL}, f, indent=1, sort_keys=True)


def fetch_all(raw, log):
    """Online run: freeze the protocol, leaderboard file, phase 1 (subAccounts + portfolio), the gate, and phase 2
    (fills, funding, candles) only if the gate passes. Write-once: keys already in raw/ are not fetched again."""
    freeze_protocol(raw)
    st = Store(raw, online=True)
    log('start used=%d' % st.used)
    try:
        st.get('leaderboard/leaderboard', LB_URL)   # the original run fetched it before the protocol freeze
        fetch_phase1(st, log)
        u = universe(st)
        traders = build_traders(st, u)
        _, gate = primary(traders)
        log('gate ' + jdump(gate))
        if gate['pass']: followability(st, traders, gate)     # phase 2 only behind the gate
    except Abort as e:
        log('ABORT ' + str(e))
    log('done new=%d used=%d' % (st.new, st.used))
    return st


def report_bytes(rep): return (json.dumps(rep, indent=1, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')


def headline_lines(rep):
    """A few plain lines from a report (full or published copy)."""
    h = rep['headline']; p = h['primary_perp_roi_A3_B4']; lp = h['leaderboard_pit_top200_next_month']
    f = lambda x, fmt='%+.2f': 'n/a' if x is None else fmt % x
    pts = lambda x: 'n/a' if x is None else '%+.1f' % (100 * x)
    pr = h['rho_by_adjacent_pair']
    return [
        'data: %s' % rep['leaderboard']['url'],
        'verdict: %s (persistence gate %s)' % (h['verdict'], 'passed' if h['persistence_gate_passed'] else 'failed'),
        'primary test, perp ROI, block 3 -> block 4, n = %s traders' % p['n'],
        '  rank correlation (Spearman)       %s   95%% CI %s .. %s' % (f(p['rho']), f(p['rho_ci'][0]), f(p['rho_ci'][1])),
        '  top decile positive in block 4    %s vs %s of all traders' % (p['top_decile_positive_B'], p['all_positive_B']),
        '  excess share positive, points     %s   95%% CI %s .. %s' % (pts(p['excess']), pts(p['excess_ci'][0]), pts(p['excess_ci'][1])),
        'rank correlation by block pair      1->2 %s   2->3 %s   3->4 %s' % (f(pr.get('1->2')), f(pr.get('2->3')), f(pr.get('3->4'))),
        'leaderboard top 200 positive next month %s%% vs %s%% of active rows, excess 95%% CI %s .. %s points' % (
            f(lp['share_positive'] and 100 * lp['share_positive'], '%.1f'), f(lp['active_base'] and 100 * lp['active_base'], '%.1f'),
            pts(lp['excess_ci'][0]), pts(lp['excess_ci'][1])),
        'follower simulation: %s' % h['follower_simulation'],
    ]


def main():
    ap = argparse.ArgumentParser(description='Hyperliquid leaderboard persistence study (read-only public data).')
    ap.add_argument('--fetch', action='store_true', help='online: fetch the leaderboard, portfolios and (gate permitting) fills')
    ap.add_argument('--fetch-docs', action='store_true', help='online: save the cited documentation pages')
    ap.add_argument('--offline', action='store_true', help='no network: build the report from raw/, or verify an existing one')
    ap.add_argument('--summary', metavar='REPORT', help='print the headline of a report JSON and exit')
    ap.add_argument('--root', default=os.environ.get('HL_STUDY_ROOT') or HERE, help='folder that holds raw/ (default: %(default)s)')
    ap.add_argument('--out', default=None, help='report path (default: <root>/%s)' % OUT_NAME)
    a = ap.parse_args()
    if a.summary:
        with open(a.summary, encoding='utf-8') as f: print('\n'.join(headline_lines(json.load(f))))
        return
    if not (a.fetch or a.fetch_docs or a.offline): ap.error('choose --fetch, --fetch-docs, --offline or --summary')
    raw = os.path.join(a.root, 'raw')
    log = lambda s: print(time.strftime('%H:%M:%S'), s, flush=True)
    if a.fetch_docs:
        st = Store(raw, online=True)
        for name, (url, _) in sorted(DOC_PAGES.items()):
            st.get('docs/' + name, url, None, text=True)
        log('docs done new=%d' % st.new)
    if a.fetch:
        fetch_all(raw, log)
    if a.offline:
        if not os.path.exists(os.path.join(raw, 'protocol_frozen.json')):
            ap.error('no raw/protocol_frozen.json under %s: run make_sample.py or --fetch first' % a.root)
        block_network()
        rep = build_report(a.root)
        b = report_bytes(rep)
        out = a.out or os.path.join(a.root, OUT_NAME)
        print('\n'.join(headline_lines(rep)))
        if os.path.exists(out):               # write-once: an existing report is verified byte for byte, never rewritten
            with open(out, 'rb') as f: same = f.read() == b
            print('existing %s: %s (sha256 %s)' % (out, 'MATCH' if same else 'MISMATCH', hashlib.sha256(b).hexdigest()))
            sys.exit(0 if same else 1)
        with open(out, 'xb') as f: f.write(b)
        print('wrote %s sha256 %s' % (out, hashlib.sha256(b).hexdigest()))


if __name__ == '__main__':
    main()
