#!/usr/bin/env python3
"""Re-measure with a MINIMAL artifact (only non-reconstructable facts) + a
string-pool breakdown, to show how compact the FlatBuffer really is vs SQLite
once the redundant columns are dropped."""
import gzip, json, os, shutil, sqlite3, subprocess, sys, time
SC = os.path.dirname(os.path.abspath(__file__))
BIN = "/Users/husam/workspace/qemu-vms/libclang-lab/cidx-cpp/build-static/cidx"
FLATC = "/opt/homebrew/bin/flatc"
MIN_FBS = os.path.join(SC,"cidx_minimal.fbs")

def sz(p): return os.path.getsize(p) if os.path.exists(p) else -1
def gzb(b): return len(gzip.compress(b,6))

LINK = {"external":1,"internal":2,"no-linkage":3,"uniqueexternal":4,None:0,"":0}
ACC  = {"public":0,"protected":1,"private":2,None:0,"":0}

def build_minimal(db, out_json):
    con = sqlite3.connect(db); con.row_factory=sqlite3.Row
    pool, pidx = [], {}
    usr_bytes=[0]; name_bytes=[0]
    def intern(s, tag=None):
        if s is None: s=""
        if s not in pidx:
            pidx[s]=len(pool); pool.append(s)
            if tag=="usr": usr_bytes[0]+=len(s)
            elif tag=="name": name_bytes[0]+=len(s)
        return pidx[s]
    intern("")
    rows=con.execute("SELECT * FROM symbol ORDER BY id").fetchall()
    sid={r["id"]:i for i,r in enumerate(rows)}
    syms=[]
    for r in rows:
        flags=((r["is_definition"]or 0)|((r["is_pure"]or 0)<<1)|((r["is_static"]or 0)<<2)
               |((r["is_instantiation"]or 0)<<3)|((r["is_named_instance"]or 0)<<4)
               |((LINK.get(r["linkage"],0))<<5)|((ACC.get(r["access"],0))<<8))
        syms.append({"name_ref":intern(r["spelling"],"name"),"usr_ref":intern(r["usr"],"usr"),
                     "parent_ref":intern(r["parent_usr"],"usr"),"kind":r["kind"]or 0,"flags":flags,
                     "loc":{"file_ref":r["file_id"]or 0,"line":r["line"]or 0,"col":r["col"]or 0}})
    erows=con.execute("SELECT * FROM edge ORDER BY id").fetchall()
    edges=[{"src_local":sid.get(r["src_id"],0),"dst_local":sid.get(r["dst_id"],0),
            "kind":r["kind"],"extra":((r["base_access"]or 0)|((r["is_virtual"]or 0)<<3))} for r in erows]
    inc=[r["id"] for r in con.execute("SELECT id FROM file")]
    con.close()
    art={"header":{"artifact_version":1,"tu_file_id":0},"string_pool":pool,
         "symbols":syms,"edges":edges,"includes":inc}
    json.dump(art,open(out_json,"w"))
    return dict(symbols=len(syms),edges=len(edges),strings=len(pool),
                strbytes=sum(len(s) for s in pool),usr_bytes=usr_bytes[0],name_bytes=name_bytes[0])

def flatc_bin(fbs,jp,outdir):
    subprocess.run([FLATC,"--binary","--strict-json","-o",outdir,fbs,jp],check=True,capture_output=True)
    return os.path.join(outdir,os.path.splitext(os.path.basename(jp))[0]+".bin")

def main():
    base, work = sys.argv[1], sys.argv[2]; tus=sys.argv[3:]
    os.makedirs(work,exist_ok=True); rows=[]
    for tu in tus:
        n=os.path.basename(tu); wd=os.path.join(work,n+".db")
        if os.path.exists(wd): shutil.rmtree(wd)
        shutil.copytree(base,wd)
        env=dict(os.environ,INDEXER_CACHE=wd,CIDX_PROGRESS="0")
        subprocess.run([BIN,"index",tu],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        db=os.path.join(wd,"index.db"); db_sz=sz(db)
        jp=os.path.join(work,n+".min.json"); c=build_minimal(db,jp)
        fb=flatc_bin(MIN_FBS,jp,work); fb_sz=sz(fb); fbgz=gzb(open(fb,"rb").read())
        # USR-hashed floor: replace usr+parent strings with 8-byte hashes (not stored in pool)
        # est: pool without usr strings + 16 bytes/symbol for 2 hashes
        pool_wo_usr = c["strbytes"] - c["usr_bytes"]
        hashed_est = pool_wo_usr + c["symbols"]*16 + c["edges"]*9 + c["symbols"]*20  # rough struct overhead
        rows.append(dict(name=n,db=db_sz,fb=fb_sz,fbgz=fbgz,**c))
        shutil.rmtree(wd)
        print(f"[{n}] sqlite={db_sz//1024}K  MIN-FlatBuf={fb_sz//1024}K (gz {fbgz//1024}K)  "
              f"{c['symbols']}sym {c['edges']}edge  pool={c['strbytes']//1024}K "
              f"(usr={c['usr_bytes']//1024}K name={c['name_bytes']//1024}K)")
    print("\n===== MINIMAL artifact vs SQLite =====")
    print(f"{'TU':<24}{'sqlite':>9}{'MIN.fb':>9}{'MIN.gz':>9}{'×vs sqlite':>11}{'×gz':>7}{'pool(usr%)':>12}")
    for r in rows:
        usrpct=100*r['usr_bytes']/max(r['strbytes'],1)
        print(f"{r['name']:<24}{r['db']//1024:>8}K{r['fb']//1024:>8}K{r['fbgz']//1024:>8}K"
              f"{r['db']/r['fb']:>10.1f}×{r['db']/r['fbgz']:>6.1f}×{usrpct:>10.0f}%")
    json.dump(rows,open(os.path.join(work,"min_results.json"),"w"),indent=2)

if __name__=="__main__": main()
