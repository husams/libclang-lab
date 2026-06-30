#!/usr/bin/env python3
"""Build a REAL per-TU FlatBuffer artifact from the actual extracted facts and
compare its disk size against the full Clang AST dump, the SQLite per-TU
contribution, and the source. Uses flatc --binary (authoritative bytes).

Per TU:
  1. cidx index <TU> into a fresh DB (time it; record DB size + row counts).
  2. Export the DB's symbol/edge/edge_site/call_arg facts to schema JSON.
  3. flatc --binary cidx_artifact.fbs facts.json -> facts.bin (real FlatBuffer).
  4. clang -Xclang -ast-dump (text) and -ast-dump=json for the same TU+flags.
  5. Tabulate bytes (+gzip) and ratios.
"""
import gzip, json, os, shutil, sqlite3, subprocess, sys, time, re

SC = os.path.dirname(os.path.abspath(__file__))
FBS = os.path.join(SC, "cidx_artifact.fbs")
BIN = "/Users/husam/workspace/qemu-vms/libclang-lab/cidx-cpp/build-static/cidx"
FLATC = "/opt/homebrew/bin/flatc"

def sz(p): return os.path.getsize(p) if os.path.exists(p) else -1
def gz(p):
    if not os.path.exists(p): return -1
    return len(gzip.compress(open(p,"rb").read(), 6))

def intern(pool, idx, s):
    if s is None: s = ""
    if s not in idx:
        idx[s] = len(pool); pool.append(s)
    return idx[s]

def export_json(db, out_json):
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    pool, pidx = [], {}
    intern(pool, pidx, "")  # ref 0 = empty
    # symbols, assign local ids by row order
    symrows = con.execute("SELECT * FROM symbol ORDER BY id").fetchall()
    sid_local = {r["id"]: i for i, r in enumerate(symrows)}
    def loc(f,l,c): return {"file_ref": int(f) if f is not None else 0,
                            "line": l or 0, "col": c or 0}
    symbols = []
    for r in symrows:
        flags = ((r["is_definition"] or 0) | ((r["is_pure"] or 0)<<1) |
                 ((r["is_static"] or 0)<<2) | ((r["is_instantiation"] or 0)<<3) |
                 ((r["is_named_instance"] or 0)<<4))
        symbols.append({
            "name_ref": intern(pool,pidx,r["spelling"]),
            "usr_ref": intern(pool,pidx,r["usr"]),
            "qual_ref": intern(pool,pidx,r["qual_name"]),
            "display_ref": intern(pool,pidx,r["display_name"]),
            "type_ref": intern(pool,pidx,r["type_info"]),
            "decl_path_ref": intern(pool,pidx,r["decl_path"]),
            "parent_usr_ref": intern(pool,pidx,r["parent_usr"]),
            "linkage_ref": intern(pool,pidx,r["linkage"]),
            "access_ref": intern(pool,pidx,r["access"]),
            "kind": r["kind"] or 0, "flags": flags,
            "loc": loc(r["file_id"], r["line"], r["col"]),
            "decl_loc": loc(r["decl_file_id"], r["decl_line"], r["decl_col"]),
        })
    edgerows = con.execute("SELECT * FROM edge ORDER BY id").fetchall()
    eid_local = {r["id"]: i for i, r in enumerate(edgerows)}
    edges = [{"src_local": sid_local.get(r["src_id"],0), "dst_local": sid_local.get(r["dst_id"],0),
              "kind": r["kind"], "count": r["count"] or 1,
              "base_access": (r["base_access"] or 0), "is_virtual": (r["is_virtual"] or 0)}
             for r in edgerows]
    sites = []
    for r in con.execute("SELECT * FROM edge_site").fetchall():
        sites.append({"edge_idx": eid_local.get(r["edge_id"],0),
                      "loc": loc(r["file_id"], r["line"], r["col"]),
                      "conditional": r["conditional"] or 0,
                      "args_sig_ref": intern(pool,pidx,r["args_sig"]),
                      "recv_kind_ref": intern(pool,pidx,r["recv_src_kind"]),
                      "recv_type_usr_ref": intern(pool,pidx,r["recv_type_usr"]),
                      "recv_decl_usr_ref": intern(pool,pidx,r["recv_decl_usr"]),
                      "recv_param_pos": r["recv_param_pos"] if r["recv_param_pos"] is not None else -1})
    cargs = []
    for r in con.execute("SELECT * FROM call_arg").fetchall():
        cargs.append({"edge_idx": eid_local.get(r["edge_id"],0),
                      "loc": loc(r["file_id"], r["line"], r["col"]),
                      "position": r["position"],
                      "src_kind_ref": intern(pool,pidx,r["src_kind"]),
                      "type_usr_ref": intern(pool,pidx,r["type_usr"]),
                      "decl_usr_ref": intern(pool,pidx,r["decl_usr"]),
                      "callee_usr_ref": intern(pool,pidx,r["callee_usr"])})
    includes = [r["id"] for r in con.execute("SELECT id FROM file")]
    con.close()
    art = {"header": {"artifact_version": 1, "libclang_version": "18.1.1", "tu_file_id": 0},
           "string_pool": pool, "symbols": symbols, "edges": edges,
           "edge_sites": sites, "call_args": cargs, "includes": includes}
    json.dump(art, open(out_json,"w"))
    return dict(symbols=len(symbols), edges=len(edges), sites=len(sites),
                cargs=len(cargs), strings=len(pool),
                strbytes=sum(len(s) for s in pool))

def flatc_binary(json_path, outdir):
    subprocess.run([FLATC, "--binary", "--strict-json", "-o", outdir, FBS, json_path],
                   check=True, capture_output=True)
    base = os.path.splitext(os.path.basename(json_path))[0]
    return os.path.join(outdir, base + ".bin")

def get_flags(compile_db, tu):
    d = json.load(open(compile_db))
    for e in d:
        f = e["file"]
        if not os.path.isabs(f): f = os.path.normpath(os.path.join(e["directory"], f))
        if f == tu:
            if "arguments" in e: return e["arguments"], e["directory"]
            return e["command"].split(), e["directory"]
    return None, None

def main():
    compile_db, base_dir, work = sys.argv[1], sys.argv[2], sys.argv[3]
    tus = sys.argv[4:]
    os.makedirs(work, exist_ok=True)
    rows = []
    for tu in tus:
        name = os.path.basename(tu)
        wd = os.path.join(work, name+".db");
        if os.path.exists(wd): shutil.rmtree(wd)
        shutil.copytree(base_dir, wd)
        env = dict(os.environ, INDEXER_CACHE=wd, CIDX_PROGRESS="0")
        # warm
        subprocess.run([BIN,"index",tu], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(wd); shutil.copytree(base_dir, wd)
        t0=time.time(); subprocess.run([BIN,"index",tu], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); idx_t=time.time()-t0
        dbp = os.path.join(wd,"index.db")
        db_sz = sz(dbp)
        # export + flatc
        jp = os.path.join(work, name+".json")
        t1=time.time(); counts = export_json(dbp, jp); fbp = flatc_binary(jp, work); fb_t=time.time()-t1
        fb_sz = sz(fbp)
        # AST dumps
        flags, cwd = get_flags(compile_db, tu)
        ast_txt = os.path.join(work, name+".ast.txt"); ast_json = os.path.join(work, name+".ast.json")
        astt_sz = astj_sz = -1
        if flags:
            # strip the driver + -c/-o + the source; add -fsyntax-only -Xclang -ast-dump
            cl = [a for a in flags]
            # replace the compiler driver with clang++, drop -c and -o X
            drv = "/usr/bin/clang++"
            args = []
            skip=False
            for i,a in enumerate(cl[1:]):  # skip argv0 driver
                if skip: skip=False; continue
                if a in ("-c",): continue
                if a == "-o": skip=True; continue
                if a.endswith(".o"): continue
                args.append(a)
            try:
                with open(ast_txt,"wb") as fh:
                    subprocess.run([drv,"-fsyntax-only","-Xclang","-ast-dump"]+args,
                                   cwd=cwd, stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
                astt_sz = sz(ast_txt)
            except Exception as e: print("  ast-dump txt failed:", e)
            try:
                with open(ast_json,"wb") as fh:
                    subprocess.run([drv,"-fsyntax-only","-Xclang","-ast-dump=json"]+args,
                                   cwd=cwd, stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
                astj_sz = sz(ast_json)
            except Exception as e: print("  ast-dump json failed:", e)
        src_sz = sz(tu)
        rows.append(dict(name=name, idx_t=idx_t, fb_t=fb_t, src=src_sz, db=db_sz,
                         fb=fb_sz, fbgz=gz(fbp), astt=astt_sz, astj=astj_sz, **counts))
        print(f"[{name}] idx={idx_t:.2f}s fb_emit={fb_t:.2f}s  src={src_sz//1024}K db={db_sz//1024}K "
              f"FB={fb_sz//1024}K (gz {gz(fbp)//1024}K)  ast_txt={astt_sz//1024 if astt_sz>0 else -1}K "
              f"ast_json={astj_sz//1024 if astj_sz>0 else -1}K  [{counts['symbols']}sym {counts['edges']}edge]")

    print("\n===== DISK / COMPACTNESS =====")
    hdr = f"{'TU':<24}{'sym':>7}{'edge':>8}{'src':>9}{'sqlite':>9}{'FlatBuf':>9}{'FB.gz':>8}{'AST.txt':>10}{'AST.json':>10}"
    print(hdr)
    for r in rows:
        def k(x): return f"{x//1024}K" if x and x>0 else "-"
        print(f"{r['name']:<24}{r['symbols']:>7}{r['edges']:>8}{k(r['src']):>9}{k(r['db']):>9}"
              f"{k(r['fb']):>9}{k(r['fbgz']):>8}{k(r['astt']):>10}{k(r['astj']):>10}")
    print("\n--- ratios (× smaller than full AST text dump) ---")
    for r in rows:
        if r['astt']>0 and r['fb']>0:
            print(f"  {r['name']:<24} FlatBuffer is {r['astt']/r['fb']:.0f}× smaller than AST.txt, "
                  f"{r['astj']/r['fb']:.0f}× smaller than AST.json, "
                  f"{r['db']/r['fb']:.1f}× smaller than SQLite" if r['astj']>0 else "")
    json.dump(rows, open(os.path.join(work,"fb_results.json"),"w"), indent=2)

if __name__=="__main__": main()
