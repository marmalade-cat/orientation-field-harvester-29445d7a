#!/usr/bin/env python3
"""OrientationFieldHarvesterV1 - minimal prototype

- Accepts a DOI/URL/local PDF path
- Fetches HTML or reads PDF text (if pdftotext available)
- Finds candidate supplemental file URLs (simple heuristics)
- Downloads files, validates format (CSV/JSON/.npy) for minimal pose schema
- Produces merged timestamp-aligned CSV and provenance.json

This is intentionally small and uses only Python standard library.
"""
import sys, os, re, csv, json, time, hashlib, argparse, subprocess, tempfile, shutil
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen, Request

# helper: fetch URL simple
def fetch_url_text(url):
    req = Request(url, headers={"User-Agent":"OrientationFieldHarvester/1.0"})
    with urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', errors='ignore')

# helper: download binary
def download_binary(url, dest):
    req = Request(url, headers={"User-Agent":"OrientationFieldHarvester/1.0"})
    with urlopen(req, timeout=30) as r, open(dest, 'wb') as out:
        shutil.copyfileobj(r, out)

# extract candidate links from HTML using simple regex for common hosts and file extensions
def extract_candidate_links(base_url, html):
    links = set()
    # find hrefs
    for m in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        u = urljoin(base_url, m)
        if any(host in u for host in ['zenodo.org','figshare.com','github.com','datadryad.org','osf.io']) or re.search(r'\.(csv|json|npy|zip|tgz|tar|pdf)$', u, re.I):
            links.add(u)
    # naive DOIs converted to dx.doi
    for m in re.findall(r'(10\.\d{4,9}/[^\s"\'<>]+)', html):
        links.add('https://doi.org/'+m)
    return list(links)

# read PDF text using pdftotext if available
def pdf_to_text(path):
    try:
        out = subprocess.check_output(['pdftotext', path, '-'], stderr=subprocess.DEVNULL)
        return out.decode('utf-8', errors='ignore')
    except Exception:
        return ''

# minimal schema validation for a single record dict
def validate_record(rec):
    # required: timestamp, module_id, x,y,z
    if not all(k in rec for k in ('timestamp','module_id','x','y','z')):
        return False
    # orientation: either quaternion keys or euler keys
    if all(k in rec for k in ('qx','qy','qz','qw')) or all(k in rec for k in ('roll','pitch','yaw')):
        return True
    return False

# parse CSV: expect header names matching keys
def parse_csv(path):
    out=[]
    with open(path, newline='') as f:
        r=csv.DictReader(f)
        for row in r:
            # coerce numeric fields
            rec={}
            for k,v in row.items():
                if v is None: continue
                s=v.strip()
                # try numeric
                try:
                    val = float(s)
                    # keep ints as floats, timestamps allowed
                    rec[k]=val
                except Exception:
                    rec[k]=s
            out.append(rec)
    return out

# parse JSON: accept list of records or object with 'data'
def parse_json(path):
    with open(path,'r') as f:
        d=json.load(f)
    if isinstance(d, list):
        return d
    if isinstance(d, dict) and 'data' in d and isinstance(d['data'], list):
        return d['data']
    return []

# parse npy using numpy if present (optional): try import
def parse_npy(path):
    try:
        import numpy as _np
    except Exception:
        print('NumPy not available; skipping .npy file', file=sys.stderr)
        return []
    arr = _np.load(path, allow_pickle=True)
    # expect structured array or list of dicts
    if isinstance(arr, _np.ndarray):
        if arr.dtype.names:
            recs=[]
            for row in arr:
                recs.append({k: float(row[k]) if _np.isscalar(row[k]) else row[k] for k in arr.dtype.names})
            return recs
        if arr.dtype == object:
            return list(arr)
    return []

# compute sha256
def sha256_file(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()

# merge records by timestamp: simple alignment by taking union of timestamps and forward-fill per module
def merge_and_align(records):
    # records: list of (rec dict) with timestamp numeric
    # group by module_id
    modules = {}
    for r in records:
        try:
            t = float(r.get('timestamp'))
        except Exception:
            continue
        mid = str(r.get('module_id'))
        modules.setdefault(mid,[]).append((t,r))
    # sort each
    for mid in modules:
        modules[mid].sort(key=lambda x:x[0])
    # unified timestamps (sorted unique)
    all_ts = sorted(set(t for mid in modules for t,_ in modules[mid]))
    merged=[]
    for t in all_ts:
        row={'timestamp':t}
        for mid,seq in modules.items():
            # find latest entry <= t
            val=None
            for ts,rec in seq:
                if ts<=t:
                    val=rec
                else:
                    break
            if val:
                # prefix fields with module id
                for k,v in val.items():
                    if k=='timestamp' or k=='module_id': continue
                    row[f'{mid}_{k}']=v
        merged.append(row)
    return merged

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source', required=True, help='DOI (https://doi.org/...), web URL, or local PDF path')
    p.add_argument('--out', default='ofh_out', help='output directory')
    args=p.parse_args()

    src = args.source
    outdir = args.out
    os.makedirs(outdir, exist_ok=True)
    provenance = []
    candidates = []

    # decide: local file or URL
    if os.path.exists(src) and os.path.isfile(src):
        # local PDF or HTML
        if src.lower().endswith('.pdf'):
            text = pdf_to_text(src)
            base = 'file://' + os.path.abspath(src)
        else:
            with open(src,'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
            base = 'file://' + os.path.abspath(src)
        candidates += extract_candidate_links(base, text)
    else:
        # treat as URL; if DOI expand to doi.org
        if re.match(r'^10\.\d', src):
            src = 'https://doi.org/' + src
        try:
            html = fetch_url_text(src)
            base = src
            candidates += extract_candidate_links(base, html)
            # also look for obvious supplemental link texts
            for m in re.findall(r'\b(supplemental|supplementary|supporting)\b[^\n\r\<\>:\.]*(href=["\']([^"\']+)["\'])?', html, re.I):
                if m and m[1]:
                    candidates.append(urljoin(base,m[1]))
        except Exception as e:
            print('Failed to fetch source URL:', e, file=sys.stderr)

    # de-dup
    candidates = list(dict.fromkeys(candidates))
    print('Found candidate URLs:', candidates)

    downloaded_records = []
    for idx,u in enumerate(candidates):
        try:
            fname = os.path.basename(urlparse(u).path) or f'file{idx}'
            dest = os.path.join(outdir, fname)
            print('Downloading', u)
            download_binary(u, dest)
            ch = sha256_file(dest)
            provenance.append({'source_url':u,'local_path':dest,'sha256':ch,'retrieved_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            # try parse by extension
            ext = fname.split('.')[-1].lower()
            parsed=[]
            if ext=='csv':
                parsed = parse_csv(dest)
            elif ext=='json':
                parsed = parse_json(dest)
            elif ext=='npy':
                parsed = parse_npy(dest)
            elif ext in ('zip','tgz','tar'):
                print('Archive found; skipping archive extraction in prototype')
            else:
                # try CSV parse anyway
                try:
                    parsed = parse_csv(dest)
                except Exception:
                    parsed = []
            # validate parsed
            val_recs = []
            for r in parsed:
                if validate_record(r):
                    # normalize numeric keys to floats where possible
                    for k in list(r.keys()):
                        try:
                            r[k]=float(r[k]) if (k!='module_id') else r[k]
                        except Exception:
                            pass
                    val_recs.append(r)
            if val_recs:
                print(f'Validated {len(val_recs)} records in', fname)
                downloaded_records.extend(val_recs)
        except Exception as e:
            print('Error handling candidate', u, e, file=sys.stderr)

    # write provenance
    prov_path = os.path.join(outdir,'provenance.json')
    with open(prov_path,'w') as f:
        json.dump(provenance,f,indent=2)
    print('Wrote provenance to', prov_path)

    # merge
    if downloaded_records:
        merged = merge_and_align(downloaded_records)
        # write merged CSV
        all_keys = set(k for r in merged for k in r.keys())
        fieldnames = sorted(all_keys, key=lambda s: (s!='timestamp', s))
        out_csv = os.path.join(outdir,'merged_aligned.csv')
        with open(out_csv,'w', newline='') as f:
            w=csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in merged:
                w.writerow(r)
        print('Wrote merged CSV to', out_csv)

        # summary report
        summary = {
            'num_source_candidates': len(candidates),
            'num_validated_records': len(downloaded_records),
            'merged_rows': len(merged),
            'provenance_file': prov_path
        }
        with open(os.path.join(outdir,'summary.json'),'w') as f:
            json.dump(summary, f, indent=2)
        print('Wrote summary.json')
    else:
        print('No validated records found.')

if __name__=='__main__':
    main()
