"""Post-hoc robustness check of the pre-registered dollar view (absolute PnL instead of ROI), by block pair.

Not pre-registered: written after the primary result, to see whether the dollar view's 3->4 result also holds
for the earlier block pairs. Reads raw/ only; sockets are blocked.

  python diag_abs_posthoc.py --root demo
"""
import argparse, os

import copytrade_hl as C


def main():
    ap = argparse.ArgumentParser(description='Dollar-view persistence by block pair (post hoc, offline).')
    ap.add_argument('--root', default=os.environ.get('HL_STUDY_ROOT') or C.HERE, help='folder that holds raw/')
    a = ap.parse_args()
    C.block_network()
    st = C.Store(os.path.join(a.root, 'raw'))
    u = C.universe(st)
    tr = C.build_traders(st, u)
    for ka, kb in ((1, 2), (2, 3), (3, 4)):
        r = C.pair_report(C.sample(tr, 'perp', ka, kb, metric='dp'), with_matrix=False, with_means=False)
        if 'spearman_rho' not in r:
            print('abs-PnL %d->%d n=%d: too few traders' % (ka, kb, r['n']))
            continue
        print('abs-PnL %d->%d n=%d rho=%s %s | top %s vs all %s excess=%s %s' % (
            ka, kb, r['n'], r['spearman_rho'], [r['spearman_ci']['lo'], r['spearman_ci']['hi']],
            r['top_decile_positive_B'], r['all_positive_B'], r['excess_share_positive'], [r['excess_ci']['lo'], r['excess_ci']['hi']]))


if __name__ == '__main__':
    main()
