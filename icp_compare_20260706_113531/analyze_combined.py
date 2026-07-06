"""Combined old-vs-new ICP signal analysis across both batches (36 companies)."""
import json, os, glob
import pandas as pd

WD = open(os.path.join(os.environ['TEMP'], 'wd.txt')).read().strip()
TEMP = os.environ['TEMP']

SIGS = ['international_profile', 'company_size_complexity',
        'onboarding_training_need', 'employer_branding', 'icp_keyword_match']

MAP = {
    'international_profile': ('International business context',
                             ['Multicultural working context', 'Intercultural communication need']),
    'company_size_complexity': ('Possible onboarding need', []),
    'onboarding_training_need': ('Onboarding or employee development signal',
                                 ['Learning and development signal', 'Broader professional training need']),
    'employer_branding': ('Employer branding signal', []),
    'icp_keyword_match': ('Learning and development signal', ['Possible English training need']),
}

HOSTED = ('glassdoor.', 'instagram.', 'facebook.', 'indeed.', 'linkedin.',
          'theknowledgeacademy.', 'zoominfo.', 'youtube.', 'twitter.', 'x.com',
          'kununu.', 'ambitionbox.')


def cls_old(s):
    return 'POS' if isinstance(s, (int, float)) and s >= 2 else ('weak' if s == 1 else 'none')


def cls_new(s):
    try:
        s = float(s)
    except Exception:
        return 'none'
    return 'POS' if s == 2 else ('weak' if s == 1 else 'none')


def own_domain(urls, dom):
    if not isinstance(urls, str) or not dom:
        return False
    root = dom.replace('www.', '').split('.')[0]
    parts = [u.strip() for u in urls.split(';') if u.strip()]
    for u in parts:
        low = u.lower()
        if any(h in low for h in HOSTED):
            continue
        if root and root in low:
            return True
    return False


def load_new(path):
    xl = pd.ExcelFile(path)
    return xl.parse('Enriched Leads')


# load both batches
old = {}
for f in ['old_baseline.json', 'old_baseline_b2.json']:
    p = os.path.join(TEMP, f)
    if os.path.exists(p):
        old.update(json.load(open(p, encoding='utf-8')))

frames = []
for f in ['icp_compare_output.xlsx', 'icp_compare_output_batch2.xlsx']:
    p = os.path.join(WD, f)
    if os.path.exists(p):
        frames.append(load_new(p))
enr = pd.concat(frames, ignore_index=True).set_index('company_name')

rows = []
for name in enr.index:
    if name not in old:
        continue
    oldsd = {x['label']: x['score'] for x in (old[name].get('visible_icp_signal_scores') or [])}
    dom = enr.loc[name, 'domain']
    for sig in SIGS:
        prim, rel = MAP[sig]
        cands = [oldsd.get(prim)] + [oldsd.get(r) for r in rel]
        best = max([c for c in cands if isinstance(c, (int, float))], default=None)
        ns = enr.loc[name, 'sig_' + sig + '_score']
        urls = enr.loc[name, sig + '_evidence_urls']
        rows.append(dict(company=name, signal=sig, old=best, old_cls=cls_old(best),
                         new=ns, new_cls=cls_new(ns),
                         own_domain_url=own_domain(urls, dom), urls=urls))

df = pd.DataFrame(rows)
n_comp = df['company'].nunique()
print('Companies analysed:', n_comp, '| cells:', len(df))
print()
print('OLD positive cells:', (df.old_cls == 'POS').sum(),
      '| NEW positive cells:', (df.new_cls == 'POS').sum())

down = df[(df.old_cls == 'POS') & (df.new_cls != 'POS')]
up = df[(df.old_cls != 'POS') & (df.new_cls == 'POS')]
print('Divergences DOWN (old POS -> new not):', len(down))
print('Divergences UP   (old not -> new POS):', len(up))
print()
print('DOWN by signal:')
print(down['signal'].value_counts().to_string())
print()
# (b) proxy: among DOWN cells, how many did new retrieve *some* evidence (weak>=1 OR own-domain url)
b_weak = down[down.new_cls == 'weak']
b_own = down[down.own_domain_url]
print('DOWN cells new=weak(1) (evidence retrieved, tier under-promoted):', len(b_weak), '/', len(down))
print('DOWN cells with own-domain grounded URL (clean recoverable):', len(b_own), '/', len(down))
print()
# weak cells overall + grounding split (false-positive risk of blunt >=1 threshold)
weak = df[df.new_cls == 'weak']
print('ALL new=weak cells:', len(weak))
print('  with own-domain URL (safe to promote):', weak.own_domain_url.sum())
print('  hosted/third-party only (false-pos risk):', (~weak.own_domain_url).sum())
print('  weak employer_branding specifically:', ((weak.signal == 'employer_branding')).sum(),
      '| of which own-domain:', weak[(weak.signal == 'employer_branding')].own_domain_url.sum())
print()
# per-signal old vs new positive rates
print('Per-signal positive-rate old vs new:')
for sig in SIGS:
    sub = df[df.signal == sig]
    op = int((sub.old_cls == 'POS').sum())
    npn = int((sub.new_cls == 'POS').sum())
    print('  %-26s old POS %2d/%2d  ->  new POS %2d/%2d' % (sig, op, len(sub), npn, len(sub)))

df.to_json(os.path.join(WD, 'combined_matrix.json'), orient='records', indent=1)
print()
print('saved combined_matrix.json')
