#!/usr/bin/env python3
"""
codeintel — unified code-intelligence router
============================================
Complements two backends that answer DIFFERENT questions:

  codebase-memory-mcp (cbm)   AST/tree-sitter graph: call-level edges, Cypher,
                              cross-service HTTP routes, semantic search, risk.
                              Strong at "who calls X / what does the arch look like".
                              Has NO importance ranking, NO git co-change.

  dep_graph.py (dep)          Import-level graph + PageRank importance + git
                              co-change/hot files. Wired into verifier_stop.py.
                              Strong at "what's important / what churns together".
                              Has NO call-level edges, NO Cypher, NO cross-service.

This router sends each question to the backend that owns it, and adds two
COMBINED views neither tool produces alone:

  trace   cbm call tree, every hop annotated with dep PageRank + co-change churn
  impact  dep import-blast + co-change  ⊕  cbm call-level callers, merged

Pass-throughs:
  rank|hot|cochange|blast|deps|rdeps   -> dep_graph (importance / history)
  search|arch|cypher|snippet|schema    -> cbm (structure / call graph)

Usage:
  python codeintel.py doctor
  python codeintel.py trace <function> [--dir inbound|outbound|both] [--depth N]
  python codeintel.py impact <file>
  python codeintel.py search <name_regex> [--label Function] [--limit N]
  python codeintel.py arch
  python codeintel.py cypher "MATCH (f:Function) RETURN f.name LIMIT 5"
  python codeintel.py rank|hot [--top N]
  python codeintel.py cochange|blast|deps|rdeps <file>
"""

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HOME = os.path.expanduser("~")
DEP_DB = os.path.join(HOME, ".claude", "data", "dep_graph.sqlite")
DEP_SCRIPT = os.path.join(HOME, ".claude", "scripts", "dep_graph.py")
PY = sys.executable  # the interpreter running us; reuse for dep_graph
CBM_EXE = os.environ.get(
    "CBM_EXE",
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local")),
                 "Programs", "codebase-memory-mcp", "codebase-memory-mcp.exe"),
)

# Known source extensions, longest first so "index.ts" style guesses resolve.
EXTS = [".php", ".vue", ".tsx", ".ts", ".jsx", ".js", ".py", ".cs", ".go", ".rs", ".java"]


# ── backend shells ───────────────────────────────────────────────────────────

def cbm(tool, params=None):
    """Invoke a codebase-memory-mcp tool, return parsed JSON (or None)."""
    if not os.path.exists(CBM_EXE):
        raise SystemExit(f"cbm binary not found: {CBM_EXE}  (set CBM_EXE)")
    args = [CBM_EXE, "cli", tool]
    if params is not None:
        args.append(json.dumps(params))
    # shell=False => argv passed verbatim, no PowerShell/Bash quote mangling.
    out = subprocess.run(args, capture_output=True, text=True, timeout=120)
    body = out.stdout.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # tool emitted human text; hand it back raw
        return {"_raw": body}


def dep(*cli_args):
    """Pass through to dep_graph.py and stream its (already pretty) output."""
    sys.stdout.flush()  # subprocess writes to the raw fd; flush our buffer first
    subprocess.run([PY, DEP_SCRIPT, *cli_args])


# ── dep_graph data access (read the sqlite directly for the join) ────────────

class DepIndex:
    """In-memory view of dep_graph: file -> pagerank + co-change churn."""

    def __init__(self):
        self.project_dir = None
        self.by_relpath = {}          # rel_path -> {pagerank, churn}
        self.noext = {}               # rel_path without extension -> rel_path
        if not os.path.exists(DEP_DB):
            return
        conn = sqlite3.connect(DEP_DB)
        row = conn.execute("SELECT value FROM meta WHERE key='project_dir'").fetchone()
        self.project_dir = row[0] if row else None
        churn = {}
        for fid, total in conn.execute(
            "SELECT file_a_id, SUM(count) FROM cochanges GROUP BY file_a_id"
        ):
            churn[fid] = churn.get(fid, 0) + (total or 0)
        for fid, total in conn.execute(
            "SELECT file_b_id, SUM(count) FROM cochanges GROUP BY file_b_id"
        ):
            churn[fid] = churn.get(fid, 0) + (total or 0)
        for fid, rel, pr in conn.execute("SELECT id, rel_path, pagerank FROM files"):
            self.by_relpath[rel] = {"pagerank": pr or 0.0, "churn": churn.get(fid, 0)}
            stem = rel
            for e in EXTS:
                if stem.endswith(e):
                    stem = stem[: -len(e)]
                    break
            self.noext[stem] = rel
        conn.close()

    def file_for_qn(self, qualified_name, project):
        """Map a cbm qualified_name to a dep_graph rel_path via longest-prefix match.

        qn looks like '<project>.app.Services.Foo.Foo.method' (dot-separated).
        We strip the project prefix, then find the longest leading run of
        segments that, joined by '/', is an actual indexed file.
        """
        qn = qualified_name
        if project and qn.startswith(project + "."):
            qn = qn[len(project) + 1:]
        segs = qn.split(".")
        for i in range(len(segs), 0, -1):
            cand = "/".join(segs[:i])
            if cand in self.noext:
                return self.noext[cand]
            for e in EXTS:
                if cand + e in self.by_relpath:
                    return cand + e
        return None

    def stats_for(self, rel_path):
        return self.by_relpath.get(rel_path)


def resolve_cbm_project(dep_index):
    """Pick the cbm project whose root matches dep_graph's indexed project."""
    data = cbm("list_projects")
    projects = (data or {}).get("projects", [])
    if not projects:
        return None
    if dep_index.project_dir:
        want = dep_index.project_dir.replace("\\", "/").rstrip("/").lower()
        for p in projects:
            root = (p.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
            if root == want:
                return p["name"]
    return projects[0]["name"]  # fall back to the only/first project


def badge(stats):
    if not stats:
        return "pr   —    churn  —"
    return f"pr {stats['pagerank']:.3f} churn {stats['churn']:>3}"


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_doctor(args):
    print("codeintel backends")
    print("─" * 60)
    cbm_ok = os.path.exists(CBM_EXE)
    print(f"  cbm binary : {'OK' if cbm_ok else 'MISSING'}  {CBM_EXE}")
    if cbm_ok:
        data = cbm("list_projects") or {}
        for p in data.get("projects", []):
            print(f"     project : {p['name']}  ({p['nodes']} nodes / {p['edges']} edges)")
    dep_ok = os.path.exists(DEP_DB)
    print(f"  dep_graph  : {'OK' if dep_ok else 'MISSING'}  {DEP_DB}")
    di = DepIndex()
    if di.project_dir:
        print(f"     project : {di.project_dir}  ({len(di.by_relpath)} files)")
    proj = resolve_cbm_project(di) if cbm_ok else None
    aligned = bool(proj and di.project_dir)
    print(f"  alignment  : {'ALIGNED on ' + proj if aligned else 'check projects'}")


def cmd_trace(args):
    di = DepIndex()
    project = resolve_cbm_project(di)
    if not project:
        raise SystemExit("no cbm project indexed; run: codebase-memory-mcp cli index_repository ...")
    res = cbm("trace_path", {
        "project": project,
        "function_name": args.function,
        "direction": args.dir,
        "depth": args.depth,
        "risk_labels": True,
    }) or {}

    def render(key, title):
        nodes = res.get(key) or []
        if not nodes:
            return
        print(f"\n{title} ({len(nodes)}):")
        print(f"  {'hop':<4} {'risk':<9} {'importance/churn':<22} {'symbol'}")
        print(f"  {'─' * 74}")
        # busiest first: dep PageRank, then hop
        annotated = []
        for n in nodes:
            rel = di.file_for_qn(n.get("qualified_name", ""), project)
            st = di.stats_for(rel) if rel else None
            annotated.append((st["pagerank"] if st else -1, n.get("hop", 0), n, st, rel))
        annotated.sort(key=lambda x: (-x[0], x[1]))
        for _, _, n, st, rel in annotated:
            loc = rel or "(file?)"
            print(f"  {n.get('hop',0):<4} {n.get('risk',''):<9} {badge(st):<22} "
                  f"{n.get('name','?')}  · {loc}")

    print(f"trace {args.function}  [{args.dir}, depth {args.depth}]  project={project}")
    render("callers", "INBOUND callers")
    render("callees", "OUTBOUND callees")
    if not res.get("callers") and not res.get("callees"):
        print("  (no call edges found — try a qualified name or --dir both)")
    print("\n  importance/churn from dep_graph PageRank + git co-change; "
          "risk from cbm static analysis.")


def cmd_impact(args):
    di = DepIndex()
    project = resolve_cbm_project(di)
    target = args.file.replace("\\", "/")

    print(f"impact of changing: {target}\n")
    print("══ import-level blast radius + churn (dep_graph) ══")
    dep("blast", args.file)
    print("\n══ historical co-change partners (dep_graph) ══")
    dep("cochange", args.file)

    if not project:
        return
    # cbm call-level: callers of each function defined in this file
    print("\n══ call-level callers of this file's symbols (cbm) ══")
    syms = cbm("search_graph", {
        "project": project,
        "file_pattern": target,
        "label": "Function",
        "min_degree": 1,
        "limit": 40,
    }) or {}
    rows = syms.get("results", [])
    if not rows:
        print("  (no functions with inbound edges found in this file)")
        return
    rows.sort(key=lambda r: r.get("in_degree", 0), reverse=True)
    print(f"  {'in':<4} {'risk':<9} {'function'}")
    print(f"  {'─' * 50}")
    for r in rows[:15]:
        risk = "CRIT" if r.get("complexity", 0) >= 10 else ""
        print(f"  {r.get('in_degree',0):<4} {risk:<9} {r.get('name','?')}")
    print("\n  left = who breaks via imports + what historically co-changes;")
    print("  right = who actually calls these functions (call graph).")


def cmd_search(args):
    di = DepIndex()
    project = resolve_cbm_project(di)
    if not project:
        raise SystemExit("no cbm project indexed")
    params = {"project": project, "name_pattern": args.pattern, "limit": args.limit}
    if args.label:
        params["label"] = args.label
    res = cbm("search_graph", params) or {}
    rows = res.get("results", [])
    print(f"{res.get('total', len(rows))} matches for /{args.pattern}/  (showing {len(rows)})\n")
    print(f"  {'importance/churn':<22} {'in/out':<8} {'symbol'}")
    print(f"  {'─' * 72}")
    annotated = []
    for r in rows:
        st = di.stats_for(r.get("file_path", ""))
        annotated.append((st["pagerank"] if st else -1, r, st))
    annotated.sort(key=lambda x: -x[0])
    for _, r, st in annotated:
        io_deg = f"{r.get('in_degree',0)}/{r.get('out_degree',0)}"
        print(f"  {badge(st):<22} {io_deg:<8} {r.get('name','?')}  · {r.get('file_path','')}")


def cmd_arch(args):
    di = DepIndex()
    project = resolve_cbm_project(di)
    if not project:
        raise SystemExit("no cbm project indexed")
    data = cbm("get_architecture", {"project": project}) or {}
    print(json.dumps(data, indent=2)[:6000])


def cmd_cypher(args):
    di = DepIndex()
    project = resolve_cbm_project(di)
    if not project:
        raise SystemExit("no cbm project indexed")
    data = cbm("query_graph", {"project": project, "query": args.query}) or {}
    print(json.dumps(data, indent=2)[:6000])


def cmd_snippet(args):
    di = DepIndex()
    project = resolve_cbm_project(di)
    data = cbm("get_code_snippet", {"project": project, "qualified_name": args.name}) or {}
    print(json.dumps(data, indent=2)[:6000])


def main():
    p = argparse.ArgumentParser(description="Unified router over cbm + dep_graph")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="show both backends + alignment")

    t = sub.add_parser("trace", help="[combined] cbm call tree + dep importance/churn")
    t.add_argument("function")
    t.add_argument("--dir", choices=["inbound", "outbound", "both"], default="both")
    t.add_argument("--depth", type=int, default=3)

    i = sub.add_parser("impact", help="[combined] dep blast+churn ⊕ cbm call-level callers")
    i.add_argument("file")

    s = sub.add_parser("search", help="cbm structural search, dep-importance annotated")
    s.add_argument("pattern")
    s.add_argument("--label")
    s.add_argument("--limit", type=int, default=20)

    sub.add_parser("arch", help="cbm architecture overview")

    c = sub.add_parser("cypher", help="cbm read-only Cypher query")
    c.add_argument("query")

    sn = sub.add_parser("snippet", help="cbm source for a qualified name")
    sn.add_argument("name")

    # pass-throughs to dep_graph (importance / history — cbm can't do these)
    for name in ("rank", "hot"):
        q = sub.add_parser(name, help=f"dep_graph {name} (importance)")
        q.add_argument("--top", type=int, default=15)
    for name in ("cochange", "blast", "deps", "rdeps"):
        q = sub.add_parser(name, help=f"dep_graph {name}")
        q.add_argument("file")

    args = p.parse_args()
    if args.cmd == "doctor":
        cmd_doctor(args)
    elif args.cmd == "trace":
        cmd_trace(args)
    elif args.cmd == "impact":
        cmd_impact(args)
    elif args.cmd == "search":
        cmd_search(args)
    elif args.cmd == "arch":
        cmd_arch(args)
    elif args.cmd == "cypher":
        cmd_cypher(args)
    elif args.cmd == "snippet":
        cmd_snippet(args)
    elif args.cmd in ("rank", "hot"):
        dep(args.cmd, "--top", str(args.top))
    elif args.cmd in ("cochange", "blast", "deps", "rdeps"):
        dep(args.cmd, args.file)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
