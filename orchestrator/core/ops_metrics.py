from __future__ import annotations
import json, os, shutil, subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _ts(v):
    if not v: return None
    try: return datetime.fromisoformat(str(v).replace('Z','+00:00')).astimezone(timezone.utc)
    except ValueError: return None


def _rows(jobs_dir):
    out=[]
    for d in Path(jobs_dir).iterdir() if Path(jobs_dir).is_dir() else []:
        p=d/'calls.jsonl'
        if not p.is_file(): continue
        for line in p.read_text(encoding='utf-8',errors='ignore').splitlines():
            try: r=json.loads(line)
            except ValueError: continue
            r['_job_id']=d.name
            try:
                job=json.loads((d/'job.json').read_text(encoding='utf-8'))
            except Exception:
                job={}
            try:
                state=json.loads((d/'state.json').read_text(encoding='utf-8'))
            except Exception:
                state={}
            cfg=((state.get('runtime') or {}).get('config') or {})
            r['_worker_provider']=cfg.get('worker_provider') or job.get('worker_provider') or job.get('provider')
            out.append(r)
    return out


def _identity(r):
    provider=r.get('provider')
    route=r.get('quota_route') if isinstance(r.get('quota_route'),dict) else {}
    if provider=='gemini': return 'gemini'
    if provider=='claude': return route.get('account') or 'older_unattributed'
    if not provider or provider=='unknown': return 'older_unattributed'
    return provider


def _sum(rows):
    keys=('input_tokens','output_tokens','thinking_tokens','cache_creation_input_tokens','cache_read_input_tokens','total_tokens')
    d={'calls':len(rows),'successes':0,'failures':0,'cost_usd':0.0,**{k:0 for k in keys}}
    for r in rows:
        ok=r.get('classification')=='SUCCESS' and r.get('validated',True) is not False
        d['successes' if ok else 'failures']+=1
        d['cost_usd']+=float(r.get('total_cost_usd') or 0)
        for k in keys: d[k]+=int(r.get(k) or 0)
    d['cost_usd']=round(d['cost_usd'],6)
    d['fresh_tokens']=d['input_tokens']+d['output_tokens']+d['thinking_tokens']+d['cache_creation_input_tokens']
    d['processed_tokens']=d['fresh_tokens']+d['cache_read_input_tokens']
    return d


def _provider_performance(rows):
    gem_calls=gem_success=flex=standard=gem_to_claude=0
    reasons=Counter(); routes=Counter(); direct_claude=0
    auto_eligible=auto_direct_claude=0
    for r in rows:
        attempts=r.get('provider_attempts') or []
        providers=[a.get('provider') for a in attempts if isinstance(a,dict)]
        is_auto=(r.get('_worker_provider')=='auto')
        if is_auto: auto_eligible += 1
        gem_in_call=False
        if attempts:
            ga=[a for a in attempts if isinstance(a,dict) and a.get('provider')=='gemini']
            gem_in_call=bool(ga); flex += sum(1 for a in ga if a.get('service_tier')=='flex')
            standard += sum(1 for a in ga if a.get('service_tier')=='standard')
            if 'gemini' in providers and 'claude' in providers: gem_to_claude+=1
            elif providers==['claude']: direct_claude+=1
        elif r.get('provider')=='gemini': gem_in_call=True
        elif r.get('provider')=='claude': direct_claude+=1
        if is_auto and r.get('provider')=='claude' and not gem_in_call:
            auto_direct_claude += 1
        if gem_in_call:
            gem_calls+=1
            if r.get('provider')=='gemini' and r.get('classification')=='SUCCESS': gem_success+=1
        route=r.get('provider_route')
        if route: routes[str(route)[:120]]+=1
        for e in r.get('fallback_events') or []:
            if isinstance(e,dict) and e.get('reason'): reasons[str(e['reason'])[:120]]+=1
    return {'auto_eligible_calls':auto_eligible,'gemini_attempts':gem_calls,
            'gemini_successful_final_calls':gem_success,
            'gemini_success_rate':round(gem_success/gem_calls,3) if gem_calls else None,
            'flex_attempts':flex,'standard_attempts':standard,'gemini_to_claude_escalations':gem_to_claude,
            'direct_claude_calls':direct_claude,'auto_direct_claude_calls':auto_direct_claude,
            'top_fallback_reasons':[{'reason':k,'count':v} for k,v in reasons.most_common(5)],
            'routes':[{'route':k,'count':v} for k,v in routes.most_common(8)]}


def collect_usage_analytics(jobs_dir, quota_file, now=None):
    now=now or datetime.now(timezone.utc); rows=_rows(jobs_dir)
    q={}
    try: q=json.loads(Path(quota_file).read_text())
    except Exception: pass
    windows={'last_5h':now-timedelta(hours=5),'last_7d':now-timedelta(days=7),'all_time':datetime.min.replace(tzinfo=timezone.utc)}
    for name,a in (q.get('accounts') or {}).items():
        reset=_ts(a.get('session_reset_at'))
        if reset: windows[f'{name}_session']=reset-timedelta(hours=float((q.get('policy') or {}).get('session_period_hours') or 5))
        wb=(a.get('buckets') or {}).get('all',{})
        wreset=_ts(wb.get('reset_at') or a.get('weekly_reset_at'))
        if wreset: windows[f'{name}_week']=wreset-timedelta(days=7)
    result={}
    for label,start in windows.items():
        rr=[r for r in rows if (_ts(r.get('ts')) or datetime.min.replace(tzinfo=timezone.utc))>=start and (_ts(r.get('ts')) or now)<=now]
        by={}
        for ident in sorted({_identity(r) for r in rr}): by[ident]=_sum([r for r in rr if _identity(r)==ident])
        result[label]={'start':start.isoformat().replace('+00:00','Z'),'end':now.isoformat().replace('+00:00','Z'),'total':_sum(rr),'by_source':by,'provider_performance':_provider_performance(rr)}
    result['note']='Claude account attribution is exact only for calls logged after quota_route account logging was enabled; older Claude calls remain claude_unattributed.'
    return result


def collect_usage_meta(jobs_dir):
    rows=_rows(jobs_dir)
    times=[(_ts(r.get('ts')), r) for r in rows if _ts(r.get('ts'))]
    if not times:
        return {'last_model_call_at':None,'total_logged_calls':0}
    last=max(times,key=lambda x:x[0])[0]
    return {'last_model_call_at':last.isoformat().replace('+00:00','Z'),'total_logged_calls':len(rows)}


def _meminfo():
    vals={}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            k,v=line.split(':',1); vals[k]=int(v.strip().split()[0])*1024
    except Exception: pass
    return vals


def collect_vm_health(now=None):
    now=now or datetime.now(timezone.utc); mem=_meminfo(); disk=shutil.disk_usage('/')
    total=mem.get('MemTotal',0); avail=mem.get('MemAvailable',0); swap_total=mem.get('SwapTotal',0); swap_free=mem.get('SwapFree',0)
    try: uptime=float(Path('/proc/uptime').read_text().split()[0])
    except Exception: uptime=None
    try: load=list(os.getloadavg())
    except Exception: load=[]
    procs=[]
    try:
        p=subprocess.run(['ps','-eo','pid,comm,%cpu,%mem','--sort=-%cpu'],capture_output=True,text=True,timeout=3)
        for line in p.stdout.splitlines()[1:6]:
            parts=line.split(None,3)
            if len(parts)==4: procs.append({'pid':int(parts[0]),'name':parts[1][:60],'cpu_pct':float(parts[2]),'mem_pct':float(parts[3])})
    except Exception: pass
    sample={'sampled_at':now.isoformat().replace('+00:00','Z'),'cpu_count':os.cpu_count(),'load_1m':load[0] if load else None,'load_5m':load[1] if len(load)>1 else None,'load_15m':load[2] if len(load)>2 else None,
            'memory_total_bytes':total,'memory_available_bytes':avail,'memory_used_pct':round((1-avail/total)*100,1) if total else None,
            'swap_total_bytes':swap_total,'swap_free_bytes':swap_free,'disk_total_bytes':disk.total,'disk_free_bytes':disk.free,'disk_used_pct':round(disk.used/disk.total*100,1) if disk.total else None,
            'uptime_seconds':uptime,'top_processes':procs}
    _append_vm_history(sample)
    return sample


VM_HISTORY_FILE = Path(__file__).resolve().parents[1] / 'logs' / 'vm-health-history.jsonl'
TREND_WINDOWS = {'1h': timedelta(hours=1), '6h': timedelta(hours=6), '24h': timedelta(hours=24), '7d': timedelta(days=7), '30d': timedelta(days=30)}

def _append_vm_history(sample, path=VM_HISTORY_FILE):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    last=None
    try:
        lines=path.read_text(encoding='utf-8').splitlines()
        if lines: last=json.loads(lines[-1])
    except Exception: pass
    now=_ts(sample.get('sampled_at'))
    prev=_ts((last or {}).get('sampled_at'))
    if prev and now and (now-prev).total_seconds()<45: return
    with path.open('a',encoding='utf-8') as f: f.write(json.dumps(sample,separators=(',',':'))+'\n')
    cutoff=(now or datetime.now(timezone.utc))-timedelta(days=31)
    try:
        kept=[]
        for line in path.read_text(encoding='utf-8').splitlines():
            try: row=json.loads(line)
            except ValueError: continue
            if (_ts(row.get('sampled_at')) or cutoff)>=cutoff: kept.append(row)
        path.write_text(''.join(json.dumps(r,separators=(',',':'))+'\n' for r in kept),encoding='utf-8')
    except Exception: pass

def collect_vm_trends(now=None, path=VM_HISTORY_FILE):
    now=now or datetime.now(timezone.utc); rows=[]
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            try: rows.append(json.loads(line))
            except ValueError: pass
    except OSError: pass
    out={}
    for key,delta in TREND_WINDOWS.items():
        start=now-delta; rr=[r for r in rows if (_ts(r.get('sampled_at')) or datetime.min.replace(tzinfo=timezone.utc))>=start]
        # Keep UI payload bounded: at most ~180 points by even sampling.
        if len(rr)>180:
            step=max(1,len(rr)//180); rr=rr[::step]
            if rows and rr[-1] is not rows[-1]: rr.append(rows[-1])
        out[key]=rr
    return out

def _bucket_seconds(window_key):
    return {'1h':300,'6h':900,'24h':3600,'7d':21600,'30d':86400}[window_key]

def collect_usage_trends(jobs_dir, now=None):
    now=now or datetime.now(timezone.utc); rows=_rows(jobs_dir); result={}
    epoch=datetime(1970,1,1,tzinfo=timezone.utc)
    for key,delta in TREND_WINDOWS.items():
        start=now-delta; sec=_bucket_seconds(key); buckets=defaultdict(list)
        for r in rows:
            ts=_ts(r.get('ts'))
            if not ts or ts<start or ts>now: continue
            idx=int((ts-epoch).total_seconds()//sec); buckets[idx].append(r)
        points=[]
        first=int((start-epoch).total_seconds()//sec); last=int((now-epoch).total_seconds()//sec)
        for idx in range(first,last+1):
            rr=buckets.get(idx,[]); by={}
            for ident in sorted({_identity(r) for r in rr}): by[ident]=_sum([r for r in rr if _identity(r)==ident])
            points.append({'ts':(epoch+timedelta(seconds=idx*sec)).isoformat().replace('+00:00','Z'),'by_source':by,'total':_sum(rr),'provider_performance':_provider_performance(rr)})
        result[key]=points
    return result
