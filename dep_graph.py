#!/usr/bin/env python3
"""
Dependency Graph Builder for Claude Code
=========================================
SoulForge-inspired codebase intelligence: SQLite-backed dependency graph with
PageRank ranking, git co-change analysis, and blast radius computation.

Usage:
    python dep_graph.py index <project_dir>        Build/rebuild the graph
    python dep_graph.py rank [--top N]              Show most important files by PageRank
    python dep_graph.py blast <file>                What breaks if this file changes?
    python dep_graph.py deps <file>                 What does this file depend on?
    python dep_graph.py rdeps <file>                What depends on this file?
    python dep_graph.py cochange <file> [--top N]   Files that change together (git history)
    python dep_graph.py symbols <file>              Symbols exported by this file
    python dep_graph.py query <symbol_name>         Find a symbol across the codebase
    python dep_graph.py stats                       Graph statistics
    python dep_graph.py hot [--top N]               Hot files: high PageRank + high co-change
"""

import argparse
import io
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Where the graph lives. Override with DEP_GRAPH_DB to keep separate graphs for
# separate projects (or to avoid clobbering an existing one), e.g.
#   DEP_GRAPH_DB=./mygraph.sqlite python dep_graph.py index .
DB_PATH = os.environ.get("DEP_GRAPH_DB") or os.path.join(
    os.path.expanduser("~/.dep-graph"), "dep_graph.sqlite"
)
DB_DIR = os.path.dirname(DB_PATH) or "."

# File extensions we care about
LANG_MAP = {
    ".php": "php",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".vue": "vue",
    ".py": "python",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

IGNORE_DIRS = {
    "node_modules", "vendor", ".git", ".svn", "__pycache__", ".next",
    "dist", "build", "storage", "cache", ".claude", ".idea", ".vscode",
    "public", "bootstrap/cache",
}

IGNORE_FILES = {"package-lock.json", "composer.lock", "yarn.lock", "pnpm-lock.yaml"}


# ── Schema ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    rel_path TEXT NOT NULL,
    language TEXT,
    last_modified REAL,
    last_indexed REAL,
    symbol_count INTEGER DEFAULT 0,
    pagerank REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT,
    line INTEGER,
    scope TEXT,
    exported INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    from_file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    to_file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    symbol_name TEXT,
    line INTEGER,
    UNIQUE(from_file_id, to_file_id, symbol_name)
);

CREATE TABLE IF NOT EXISTS cochanges (
    file_a_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    file_b_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    count INTEGER DEFAULT 1,
    last_commit TEXT,
    PRIMARY KEY (file_a_id, file_b_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_file_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_file_id);
CREATE INDEX IF NOT EXISTS idx_files_rel ON files(rel_path);
"""


# ── Database ────────────────────────────────────────────────────────────────

def get_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# ── File Discovery ──────────────────────────────────────────────────────────

def discover_files(project_dir):
    """Walk project and yield (abs_path, rel_path, language)."""
    project_dir = os.path.abspath(project_dir)
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
        for fname in files:
            if fname in IGNORE_FILES:
                continue
            ext = os.path.splitext(fname)[1].lower()
            lang = LANG_MAP.get(ext)
            if lang:
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, project_dir).replace("\\", "/")
                yield abs_path, rel_path, lang


# ── Import Parsing ──────────────────────────────────────────────────────────

def parse_php_imports(content, rel_path):
    """Extract PHP use statements and require/include."""
    imports = []
    # use App\Models\User;  use App\Http\Controllers\Controller;
    for m in re.finditer(r'^\s*use\s+([\w\\]+)(?:\s+as\s+\w+)?;', content, re.MULTILINE):
        fqcn = m.group(1)
        # Convert namespace to path: App\Models\User -> app/Models/User.php
        parts = fqcn.split("\\")
        if parts[0] == "App":
            parts[0] = "app"
        path_guess = "/".join(parts) + ".php"
        imports.append((path_guess, fqcn.split("\\")[-1], m.start()))
    return imports


def parse_ts_imports(content, rel_path):
    """Extract TypeScript/JavaScript import statements."""
    imports = []
    # import { X, Y } from './path'
    # import X from './path'
    # import * as X from './path'
    # import './path'
    pattern = r"""import\s+(?:(?:\{[^}]*\}|[\w*]+(?:\s+as\s+\w+)?|\*\s+as\s+\w+)\s+from\s+)?['"]([^'"]+)['"]"""
    for m in re.finditer(pattern, content, re.MULTILINE):
        module_path = m.group(1)
        if module_path.startswith("."):
            # Extract named imports if present
            full = m.group(0)
            names_match = re.search(r'\{([^}]+)\}', full)
            if names_match:
                for name in names_match.group(1).split(","):
                    name = name.strip().split(" as ")[0].strip()
                    if name:
                        imports.append((module_path, name, m.start()))
            else:
                default_match = re.search(r'import\s+(\w+)\s+from', full)
                sym = default_match.group(1) if default_match else None
                imports.append((module_path, sym, m.start()))
    return imports


def parse_vue_imports(content, rel_path):
    """Extract imports from Vue SFC <script> blocks."""
    script_match = re.search(r'<script[^>]*>([\s\S]*?)</script>', content)
    if not script_match:
        return []
    return parse_ts_imports(script_match.group(1), rel_path)


def _py_module_to_path(module):
    """`.models` -> `./models`, `..utils.io` -> `../utils/io`, `a.b` -> `a/b`.

    Leading dots are PEP 328 relative-import levels and must survive as path
    prefixes. The old code ran `.replace(".", "/")` over the whole string, so
    `.models` became `/models` — which no longer starts with "." and therefore
    never reached the relative branch of resolve_import_path. Every relative
    import in every Python project was silently dropped (`requests`: 3 edges
    across 37 files, and `blast` reported its most-imported module as a leaf).
    """
    n = len(module) - len(module.lstrip("."))
    rest = module[n:].replace(".", "/")
    if n == 0:
        return rest
    prefix = "./" if n == 1 else "../" * (n - 1)
    return prefix + rest if rest else prefix.rstrip("/")


def parse_python_imports(content, rel_path):
    """Extract Python import/from statements."""
    imports = []
    # from foo.bar import baz, qux   /   from .models import Response
    for m in re.finditer(r'^\s*from\s+([\w.]+)\s+import\s+(.+)', content, re.MULTILINE):
        module = m.group(1)
        names = m.group(2)
        for name in names.split(","):
            name = name.strip().split(" as ")[0].strip().rstrip(")").strip()
            if name and name != "*":
                imports.append((_py_module_to_path(module), name, m.start()))
    # import foo.bar
    for m in re.finditer(r'^\s*import\s+([\w.]+)', content, re.MULTILINE):
        module = m.group(1)
        imports.append((module.replace(".", "/"), module.split(".")[-1], m.start()))
    return imports


PARSERS = {
    "php": parse_php_imports,
    "typescript": parse_ts_imports,
    "javascript": parse_ts_imports,
    "vue": parse_vue_imports,
    "python": parse_python_imports,
}


# ── Symbol Extraction (regex-based, fast) ───────────────────────────────────

def extract_symbols(content, language):
    """Extract exported symbols from file content."""
    symbols = []

    if language == "php":
        for m in re.finditer(r'^\s*(?:abstract\s+|final\s+)?class\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "class", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*interface\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "interface", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*trait\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "trait", content[:m.start()].count("\n") + 1, True))
        # Modifiers repeat and combine: `public static`, `final public`,
        # `abstract protected`, `readonly`. The old single-modifier pattern made
        # every combination invisible — measured on a large PHP codebase:
        # 6,305 methods found vs 6,607 real, so 302 (5%) could not be found by
        # `dep_graph query` at all.
        # NOTE the trailing + not *: with * this also matches a bare
        # `function helper()`, which the plain-function rule below already
        # catches, so every top-level function would be indexed twice.
        for m in re.finditer(r'^\s*(?:(?:public|protected|private|static|final|abstract|readonly)\s+)+function\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "method", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*function\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "function", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*enum\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "enum", content[:m.start()].count("\n") + 1, True))

    elif language in ("typescript", "javascript"):
        for m in re.finditer(r'^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "function", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*export\s+(?:default\s+)?class\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "class", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*export\s+(?:default\s+)?interface\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "interface", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*export\s+(?:default\s+)?type\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "type", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*export\s+(?:const|let|var)\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "variable", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^\s*export\s+enum\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "enum", content[:m.start()].count("\n") + 1, True))
        # Non-exported (still useful for internal references)
        for m in re.finditer(r'^\s*(?:async\s+)?function\s+(\w+)', content, re.MULTILINE):
            name = m.group(1)
            if not any(s[0] == name for s in symbols):
                symbols.append((name, "function", content[:m.start()].count("\n") + 1, False))
        for m in re.finditer(r'^\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(', content, re.MULTILINE):
            name = m.group(1)
            if not any(s[0] == name for s in symbols):
                symbols.append((name, "function", content[:m.start()].count("\n") + 1, False))

    elif language == "vue":
        script_match = re.search(r'<script[^>]*>([\s\S]*?)</script>', content)
        if script_match:
            offset = content[:script_match.start()].count("\n")
            script_content = script_match.group(1)
            for sym in extract_symbols(script_content, "typescript"):
                symbols.append((sym[0], sym[1], sym[2] + offset, sym[3]))
            # defineProps, defineEmits, defineExpose are Vue-specific exports
            for m in re.finditer(r'(?:const\s+(\w+)\s*=\s*)?define(Props|Emits|Expose)', script_content):
                name = m.group(1) or f"define{m.group(2)}"
                symbols.append((name, "vue-api", script_content[:m.start()].count("\n") + 1 + offset, True))

    elif language == "python":
        for m in re.finditer(r'^class\s+(\w+)', content, re.MULTILINE):
            symbols.append((m.group(1), "class", content[:m.start()].count("\n") + 1, True))
        for m in re.finditer(r'^def\s+(\w+)', content, re.MULTILINE):
            name = m.group(1)
            exported = not name.startswith("_")
            symbols.append((name, "function", content[:m.start()].count("\n") + 1, exported))
        for m in re.finditer(r'^(\w+)\s*=', content, re.MULTILINE):
            name = m.group(1)
            if name.isupper() and not name.startswith("_"):
                symbols.append((name, "constant", content[:m.start()].count("\n") + 1, True))

    return symbols


# ── Import Resolution ───────────────────────────────────────────────────────

def resolve_import_path(module_path, from_rel_path, all_rel_paths):
    """Try to resolve an import path to an actual file in the project."""
    from_dir = os.path.dirname(from_rel_path)

    # For relative imports (./foo, ../bar)
    if module_path.startswith("."):
        candidate_base = os.path.normpath(os.path.join(from_dir, module_path)).replace("\\", "/")
    else:
        candidate_base = module_path

    # Try various extensions and index files
    candidates = [
        candidate_base,
        candidate_base + ".ts",
        candidate_base + ".tsx",
        candidate_base + ".js",
        candidate_base + ".jsx",
        candidate_base + ".vue",
        candidate_base + ".php",
        candidate_base + ".py",
        candidate_base + "/index.ts",
        candidate_base + "/index.tsx",
        candidate_base + "/index.js",
        candidate_base + "/index.jsx",
        # a Python package directory resolves to its __init__
        candidate_base + "/__init__.py",
    ]

    rel_set = set(all_rel_paths)
    for c in candidates:
        normalized = c.replace("\\", "/")
        if normalized in rel_set:
            return normalized

    return None


# ── PageRank ────────────────────────────────────────────────────────────────

def compute_pagerank(conn, damping=0.85, iterations=30, tol=1e-6):
    """Compute PageRank for all files based on import edges."""
    files = {row[0]: row[1] for row in conn.execute("SELECT id, rel_path FROM files").fetchall()}
    if not files:
        return

    n = len(files)
    file_ids = list(files.keys())
    id_to_idx = {fid: i for i, fid in enumerate(file_ids)}

    # Build adjacency: who links TO each file
    inlinks = defaultdict(list)
    outcount = defaultdict(int)
    for from_id, to_id in conn.execute("SELECT DISTINCT from_file_id, to_file_id FROM edges").fetchall():
        if from_id in id_to_idx and to_id in id_to_idx:
            inlinks[id_to_idx[to_id]].append(id_to_idx[from_id])
            outcount[id_to_idx[from_id]] += 1

    # Initialize
    rank = [1.0 / n] * n

    for _ in range(iterations):
        new_rank = [(1 - damping) / n] * n
        for i in range(n):
            for j in inlinks[i]:
                out = outcount[j]
                if out > 0:
                    new_rank[i] += damping * rank[j] / out

        # Check convergence
        diff = sum(abs(new_rank[i] - rank[i]) for i in range(n))
        rank = new_rank
        if diff < tol:
            break

    # Normalize to 0-1 range
    max_rank = max(rank) if rank else 1
    if max_rank > 0:
        rank = [r / max_rank for r in rank]

    # Update DB
    conn.executemany(
        "UPDATE files SET pagerank = ? WHERE id = ?",
        [(rank[i], file_ids[i]) for i in range(n)]
    )
    conn.commit()


# ── Git Co-Change Analysis ──────────────────────────────────────────────────

def analyze_git_cochanges(conn, project_dir, max_commits=500):
    """Analyze git history to find files that change together."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_commits}", "--name-only", "--pretty=format:---COMMIT %H---"],
            cwd=project_dir, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0

    # Get file_id lookup
    file_lookup = {}
    for fid, rel_path in conn.execute("SELECT id, rel_path FROM files").fetchall():
        file_lookup[rel_path] = fid

    # Parse commits
    pair_counts = defaultdict(int)
    pair_last_commit = {}
    current_commit = None
    current_files = []
    commit_count = 0

    for line in result.stdout.split("\n"):
        line = line.strip()
        if line.startswith("---COMMIT "):
            if current_files and len(current_files) <= 30:  # skip huge commits
                file_ids = [file_lookup[f] for f in current_files if f in file_lookup]
                for i, a in enumerate(file_ids):
                    for b in file_ids[i+1:]:
                        key = (min(a, b), max(a, b))
                        pair_counts[key] += 1
                        pair_last_commit[key] = current_commit
                commit_count += 1
            current_commit = line.split()[1][:12] if len(line.split()) > 1 else None
            current_files = []
        elif line:
            current_files.append(line.replace("\\", "/"))

    # Process last commit
    if current_files and len(current_files) <= 30:
        file_ids = [file_lookup[f] for f in current_files if f in file_lookup]
        for i, a in enumerate(file_ids):
            for b in file_ids[i+1:]:
                key = (min(a, b), max(a, b))
                pair_counts[key] += 1
                pair_last_commit[key] = current_commit

    # Store top co-changes (skip pairs with count=1)
    conn.execute("DELETE FROM cochanges")
    rows = [(a, b, count, pair_last_commit.get((a, b), ""))
            for (a, b), count in pair_counts.items() if count >= 2]
    conn.executemany(
        "INSERT OR REPLACE INTO cochanges (file_a_id, file_b_id, count, last_commit) VALUES (?, ?, ?, ?)",
        rows
    )
    conn.commit()
    return commit_count


# ── Indexing ────────────────────────────────────────────────────────────────

def index_project(project_dir):
    """Build the full dependency graph for a project."""
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        print(f"Error: {project_dir} is not a directory")
        sys.exit(1)

    conn = get_db()
    start = time.time()

    # Store project dir
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('project_dir', ?)", (project_dir,))

    # Discover files
    print(f"Scanning {project_dir}...")
    file_list = list(discover_files(project_dir))
    print(f"  Found {len(file_list)} source files")

    # Upsert files
    all_rel_paths = []
    for abs_path, rel_path, lang in file_list:
        mtime = os.path.getmtime(abs_path)
        conn.execute("""
            INSERT INTO files (path, rel_path, language, last_modified, last_indexed)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                language=excluded.language,
                last_modified=excluded.last_modified,
                last_indexed=excluded.last_indexed
        """, (abs_path, rel_path, lang, mtime, time.time()))
        all_rel_paths.append(rel_path)
    conn.commit()

    # Clean up deleted files
    placeholders = ",".join(["?"] * len(all_rel_paths)) if all_rel_paths else "''"
    if all_rel_paths:
        conn.execute(f"DELETE FROM files WHERE rel_path NOT IN ({placeholders})", all_rel_paths)
    conn.commit()

    # Build file ID lookup
    file_id_by_rel = {}
    for fid, rel_path in conn.execute("SELECT id, rel_path FROM files").fetchall():
        file_id_by_rel[rel_path] = fid

    # Parse all files
    print("  Parsing imports and symbols...")
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM symbols")

    total_symbols = 0
    total_edges = 0

    for abs_path, rel_path, lang in file_list:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except (IOError, OSError):
            continue

        from_id = file_id_by_rel.get(rel_path)
        if not from_id:
            continue

        # Extract symbols
        syms = extract_symbols(content, lang)
        for name, kind, line, exported in syms:
            conn.execute(
                "INSERT INTO symbols (file_id, name, kind, line, exported) VALUES (?, ?, ?, ?, ?)",
                (from_id, name, kind, line, 1 if exported else 0)
            )
        total_symbols += len(syms)
        conn.execute("UPDATE files SET symbol_count = ? WHERE id = ?", (len(syms), from_id))

        # Parse imports
        parser = PARSERS.get(lang)
        if not parser:
            continue

        imports = parser(content, rel_path)
        for module_path, symbol_name, offset in imports:
            resolved = resolve_import_path(module_path, rel_path, all_rel_paths)
            if resolved and resolved in file_id_by_rel:
                to_id = file_id_by_rel[resolved]
                if from_id != to_id:
                    line_no = content[:offset].count("\n") + 1
                    conn.execute("""
                        INSERT OR IGNORE INTO edges (from_file_id, to_file_id, symbol_name, line)
                        VALUES (?, ?, ?, ?)
                    """, (from_id, to_id, symbol_name, line_no))
                    total_edges += 1

    conn.commit()
    print(f"  Extracted {total_symbols} symbols, {total_edges} import edges")

    # Compute PageRank
    print("  Computing PageRank...")
    compute_pagerank(conn)

    # Git co-change analysis
    print("  Analyzing git co-change history...")
    commits = analyze_git_cochanges(conn, project_dir)
    print(f"  Processed {commits} commits")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s — {len(file_list)} files, {total_symbols} symbols, {total_edges} edges")
    conn.close()


# ── Query Commands ──────────────────────────────────────────────────────────

def get_project_dir(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='project_dir'").fetchone()
    return row[0] if row else None


def cmd_rank(args):
    """Show files ranked by PageRank."""
    conn = get_db()
    top = args.top or 20
    rows = conn.execute(
        "SELECT rel_path, pagerank, symbol_count, language FROM files ORDER BY pagerank DESC LIMIT ?",
        (top,)
    ).fetchall()

    if not rows:
        print("No files indexed. Run: dep_graph.py index <project_dir>")
        return

    print(f"{'Rank':<5} {'PageRank':<10} {'Symbols':<8} {'Lang':<12} {'File'}")
    print("─" * 80)
    for i, (path, pr, syms, lang) in enumerate(rows, 1):
        bar = "█" * int(pr * 20)
        print(f"{i:<5} {pr:<10.4f} {syms:<8} {lang:<12} {path}")


def cmd_blast(args):
    """Show blast radius: everything affected by changing a file."""
    conn = get_db()
    target = args.file.replace("\\", "/")

    # Find the file
    row = conn.execute("SELECT id, rel_path FROM files WHERE rel_path LIKE ?", (f"%{target}%",)).fetchone()
    if not row:
        print(f"File not found: {target}")
        return

    file_id, rel_path = row
    print(f"Blast radius for: {rel_path}\n")

    # BFS through reverse dependencies
    visited = {file_id}
    frontier = [file_id]
    depth_map = {file_id: 0}

    while frontier:
        next_frontier = []
        for fid in frontier:
            dependents = conn.execute(
                "SELECT DISTINCT from_file_id FROM edges WHERE to_file_id = ?", (fid,)
            ).fetchall()
            for (dep_id,) in dependents:
                if dep_id not in visited:
                    visited.add(dep_id)
                    next_frontier.append(dep_id)
                    depth_map[dep_id] = depth_map[fid] + 1
        frontier = next_frontier

    visited.discard(file_id)
    if not visited:
        print("  No files depend on this file (leaf node)")
        return

    # Sort by depth then pagerank
    results = []
    for fid in visited:
        row = conn.execute("SELECT rel_path, pagerank FROM files WHERE id = ?", (fid,)).fetchone()
        if row:
            results.append((depth_map[fid], row[1], row[0]))

    results.sort(key=lambda x: (x[0], -x[1]))

    print(f"  {len(results)} files affected:\n")
    print(f"  {'Depth':<7} {'PageRank':<10} {'File'}")
    print(f"  {'─'*60}")
    for depth, pr, path in results[:50]:
        indent = "  " + "→ " * depth
        print(f"  {depth:<7} {pr:<10.4f} {indent}{path}")

    if len(results) > 50:
        print(f"\n  ... and {len(results) - 50} more")


def cmd_deps(args):
    """Show what a file depends on."""
    conn = get_db()
    target = args.file.replace("\\", "/")

    row = conn.execute("SELECT id, rel_path FROM files WHERE rel_path LIKE ?", (f"%{target}%",)).fetchone()
    if not row:
        print(f"File not found: {target}")
        return

    file_id, rel_path = row
    print(f"Dependencies of: {rel_path}\n")

    deps = conn.execute("""
        SELECT DISTINCT f.rel_path, f.pagerank, e.symbol_name
        FROM edges e JOIN files f ON e.to_file_id = f.id
        WHERE e.from_file_id = ?
        ORDER BY f.pagerank DESC
    """, (file_id,)).fetchall()

    if not deps:
        print("  No dependencies (standalone file)")
        return

    print(f"  {'PageRank':<10} {'Symbol':<30} {'File'}")
    print(f"  {'─'*70}")
    for path, pr, sym in deps:
        print(f"  {pr:<10.4f} {(sym or '*'):<30} {path}")


def cmd_rdeps(args):
    """Show what depends on a file (direct reverse dependencies only)."""
    conn = get_db()
    target = args.file.replace("\\", "/")

    row = conn.execute("SELECT id, rel_path FROM files WHERE rel_path LIKE ?", (f"%{target}%",)).fetchone()
    if not row:
        print(f"File not found: {target}")
        return

    file_id, rel_path = row
    print(f"Reverse dependencies of: {rel_path}\n")

    rdeps = conn.execute("""
        SELECT DISTINCT f.rel_path, f.pagerank, e.symbol_name
        FROM edges e JOIN files f ON e.from_file_id = f.id
        WHERE e.to_file_id = ?
        ORDER BY f.pagerank DESC
    """, (file_id,)).fetchall()

    if not rdeps:
        print("  Nothing depends on this file")
        return

    print(f"  {len(rdeps)} files depend on this:\n")
    print(f"  {'PageRank':<10} {'Imports':<30} {'File'}")
    print(f"  {'─'*70}")
    for path, pr, sym in rdeps:
        print(f"  {pr:<10.4f} {(sym or '*'):<30} {path}")


def cmd_cochange(args):
    """Show files that frequently change together."""
    conn = get_db()
    target = args.file.replace("\\", "/")
    top = args.top or 15

    row = conn.execute("SELECT id, rel_path FROM files WHERE rel_path LIKE ?", (f"%{target}%",)).fetchone()
    if not row:
        print(f"File not found: {target}")
        return

    file_id, rel_path = row
    print(f"Co-change partners for: {rel_path}\n")

    rows = conn.execute("""
        SELECT f.rel_path, c.count, c.last_commit
        FROM cochanges c
        JOIN files f ON f.id = CASE WHEN c.file_a_id = ? THEN c.file_b_id ELSE c.file_a_id END
        WHERE c.file_a_id = ? OR c.file_b_id = ?
        ORDER BY c.count DESC
        LIMIT ?
    """, (file_id, file_id, file_id, top)).fetchall()

    if not rows:
        print("  No co-change history found")
        return

    print(f"  {'Count':<8} {'Last Commit':<14} {'File'}")
    print(f"  {'─'*60}")
    for path, count, commit in rows:
        print(f"  {count:<8} {commit:<14} {path}")


def cmd_symbols(args):
    """Show symbols in a file."""
    conn = get_db()
    target = args.file.replace("\\", "/")

    row = conn.execute("SELECT id, rel_path FROM files WHERE rel_path LIKE ?", (f"%{target}%",)).fetchone()
    if not row:
        print(f"File not found: {target}")
        return

    file_id, rel_path = row
    print(f"Symbols in: {rel_path}\n")

    syms = conn.execute(
        "SELECT name, kind, line, exported FROM symbols WHERE file_id = ? ORDER BY line",
        (file_id,)
    ).fetchall()

    if not syms:
        print("  No symbols found")
        return

    print(f"  {'Line':<8} {'Kind':<12} {'Exported':<10} {'Name'}")
    print(f"  {'─'*50}")
    for name, kind, line, exported in syms:
        exp = "✓" if exported else ""
        print(f"  {line:<8} {kind:<12} {exp:<10} {name}")


def cmd_query(args):
    """Find a symbol across the codebase."""
    conn = get_db()
    name = args.symbol_name

    rows = conn.execute("""
        SELECT s.name, s.kind, s.line, s.exported, f.rel_path, f.pagerank
        FROM symbols s JOIN files f ON s.file_id = f.id
        WHERE s.name = ? OR s.name LIKE ?
        ORDER BY f.pagerank DESC, s.exported DESC
        LIMIT 30
    """, (name, f"%{name}%")).fetchall()

    if not rows:
        print(f"Symbol '{name}' not found")
        return

    print(f"Found {len(rows)} matches for '{name}':\n")
    print(f"  {'PageRank':<10} {'Kind':<12} {'Line':<8} {'Exp':<5} {'File'}")
    print(f"  {'─'*70}")
    for sname, kind, line, exported, path, pr in rows:
        exp = "✓" if exported else ""
        display_name = sname if sname == name else sname
        print(f"  {pr:<10.4f} {kind:<12} {line:<8} {exp:<5} {path}  ({display_name})")


def cmd_stats(args):
    """Show graph statistics."""
    conn = get_db()
    project_dir = get_project_dir(conn)

    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    cochanges = conn.execute("SELECT COUNT(*) FROM cochanges").fetchone()[0]

    lang_counts = conn.execute(
        "SELECT language, COUNT(*) FROM files GROUP BY language ORDER BY COUNT(*) DESC"
    ).fetchall()

    top_pr = conn.execute(
        "SELECT rel_path, pagerank FROM files ORDER BY pagerank DESC LIMIT 5"
    ).fetchall()

    most_deps = conn.execute("""
        SELECT f.rel_path, COUNT(*) as cnt
        FROM edges e JOIN files f ON e.from_file_id = f.id
        GROUP BY e.from_file_id ORDER BY cnt DESC LIMIT 5
    """).fetchall()

    most_dependents = conn.execute("""
        SELECT f.rel_path, COUNT(*) as cnt
        FROM edges e JOIN files f ON e.to_file_id = f.id
        GROUP BY e.to_file_id ORDER BY cnt DESC LIMIT 5
    """).fetchall()

    print(f"Dependency Graph Statistics")
    print(f"{'─'*50}")
    if project_dir:
        print(f"  Project:    {project_dir}")
    print(f"  Files:      {files}")
    print(f"  Symbols:    {symbols}")
    print(f"  Edges:      {edges}")
    print(f"  Co-changes: {cochanges}")

    if lang_counts:
        print(f"\n  Languages:")
        for lang, count in lang_counts:
            print(f"    {lang:<15} {count} files")

    if top_pr:
        print(f"\n  Top 5 by PageRank:")
        for path, pr in top_pr:
            print(f"    {pr:.4f}  {path}")

    if most_deps:
        print(f"\n  Most dependencies (imports most):")
        for path, cnt in most_deps:
            print(f"    {cnt:<4} imports  {path}")

    if most_dependents:
        print(f"\n  Most depended on (imported by most):")
        for path, cnt in most_dependents:
            print(f"    {cnt:<4} dependents  {path}")


def cmd_hot(args):
    """Show 'hot' files: high PageRank AND high co-change frequency."""
    conn = get_db()
    top = args.top or 15

    file_rows = conn.execute(
        "SELECT id, rel_path, pagerank FROM files WHERE pagerank > 0"
    ).fetchall()

    if not file_rows:
        print("No files indexed. Run: dep_graph.py index <project_dir>")
        return

    # Compute co-change score per file
    change_scores = {}
    for fid, path, pr in file_rows:
        row = conn.execute("""
            SELECT COALESCE(SUM(count), 0) FROM cochanges
            WHERE file_a_id = ? OR file_b_id = ?
        """, (fid, fid)).fetchone()
        change_scores[fid] = row[0]

    max_change = max(change_scores.values()) if change_scores else 1
    if max_change == 0:
        max_change = 1

    # Combined score: 60% PageRank + 40% co-change frequency
    scored = []
    for fid, path, pr in file_rows:
        change_norm = change_scores[fid] / max_change
        hot_score = pr * 0.6 + change_norm * 0.4
        scored.append((hot_score, pr, change_scores[fid], path))

    scored.sort(reverse=True)

    print(f"{'Rank':<5} {'Hot':<8} {'PR':<8} {'CoChg':<7} {'File'}")
    print("─" * 70)
    for i, (hot, pr, cc, path) in enumerate(scored[:top], 1):
        print(f"{i:<5} {hot:<8.3f} {pr:<8.4f} {cc:<7} {path}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Dependency Graph Builder — SoulForge-style codebase intelligence for Claude Code"
    )
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="Build/rebuild the dependency graph")
    p_index.add_argument("project_dir", help="Root directory of the project")

    p_rank = sub.add_parser("rank", help="Show files by PageRank importance")
    p_rank.add_argument("--top", type=int, default=20)

    p_blast = sub.add_parser("blast", help="Blast radius: what breaks if this file changes")
    p_blast.add_argument("file", help="File path (partial match OK)")

    p_deps = sub.add_parser("deps", help="What does this file depend on")
    p_deps.add_argument("file")

    p_rdeps = sub.add_parser("rdeps", help="What depends on this file")
    p_rdeps.add_argument("file")

    p_cochange = sub.add_parser("cochange", help="Files that change together")
    p_cochange.add_argument("file")
    p_cochange.add_argument("--top", type=int, default=15)

    p_symbols = sub.add_parser("symbols", help="List symbols in a file")
    p_symbols.add_argument("file")

    p_query = sub.add_parser("query", help="Find a symbol across the codebase")
    p_query.add_argument("symbol_name")

    sub.add_parser("stats", help="Graph statistics")

    p_hot = sub.add_parser("hot", help="Hot files: high importance + high change frequency")
    p_hot.add_argument("--top", type=int, default=15)

    args = parser.parse_args()

    commands = {
        "index": lambda: index_project(args.project_dir),
        "rank": lambda: cmd_rank(args),
        "blast": lambda: cmd_blast(args),
        "deps": lambda: cmd_deps(args),
        "rdeps": lambda: cmd_rdeps(args),
        "cochange": lambda: cmd_cochange(args),
        "symbols": lambda: cmd_symbols(args),
        "query": lambda: cmd_query(args),
        "stats": lambda: cmd_stats(args),
        "hot": lambda: cmd_hot(args),
    }

    if args.command in commands:
        commands[args.command]()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
