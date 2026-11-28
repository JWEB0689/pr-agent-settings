#!/usr/bin/env python3
"""
Auto-PR Creator — original engine written for Jeremy, Nov 26 2026.

Modes:
  issue   — a GitHub Issue (opened, or labelled `auto-pr` / `autofix`) becomes
            a fix branch + PR. Body/comments are the spec.
  failure — ANY workflow run that completes with failure on the default
            branch (push, schedule, workflow_dispatch, PR merge, etc.)
            becomes a fix branch + PR. Extends the old scheduled-only mode.

Guardrails (same as Jeremy's existing system):
  - Never pushes to the default branch, never merges.
  - Never opens a duplicate PR for the same issue / run.
  - Skips infra failures (no runner, rate limit, disk full, cancelled infra).
  - Caps scope: Gemini is asked only for files implicated by the issue/logs.
  - Verifies with the repo's test command when discoverable; on a genuine
    test failure it rolls back and opens no PR. Missing test runner =
    land unverified with a warning (fixed Nov 25 rule).

Env: GH_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, REPO, MODE,
     ISSUE_NUMBER (issue mode), RUN_ID / WORKFLOW_NAME / RUN_URL / HEAD_SHA (failure mode),
     DRY_RUN=1 for local tests (no GitHub writes, no Gemini call).
"""
import ast, json, os, re, subprocess, sys, time, urllib.request, urllib.error

API = "https://api.github.com"
INFRA_PATTERNS = [
    r"no runner", r"runner.*not found", r"rate limit", r"disk.*full",
    r"no space left", r"service unavailable", r"internal error.*github",
    r"the operation was canceled", r"missing.*secret", r"permission denied.*token",
]

def slugify(text: str, limit: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "fix").lower()).strip("-")
    return (s[:limit].rstrip("-") or "fix")

def is_infra_failure(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in INFRA_PATTERNS)

def branch_for_issue(issue_number: int, title: str) -> str:
    return f"autofix/issue-{issue_number}-{slugify(title)}"

def branch_for_run(workflow_name: str, run_id: str) -> str:
    return f"autofix/run-{slugify(workflow_name)}-{str(run_id)[-6:]}"

def gh(method: str, path: str, payload=None):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    req.add_header("Accept", "application/vnd.github+json")
    data = json.dumps(payload).encode() if payload is not None else None
    if data: req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data) as r:
        return json.loads(r.read() or b"{}")

def open_pr_exists(repo: str, branch: str) -> bool:
    if os.environ.get("DRY_RUN") == "1":
        return False
    prs = gh("GET", f"/repos/{repo}/pulls?state=open&head={repo.split('/')[0]}:{branch}")
    return len(prs) > 0

DEFAULT_FALLBACKS = "gemini-2.5-flash,gemini-flash-lite-latest"
LAST_MODEL_USED = ""

def model_chain(first_model: str | None = None) -> list:
    primary = first_model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS)
    chain = [primary] + [m.strip() for m in fallbacks.split(",") if m.strip()]
    return list(dict.fromkeys(chain))

def call_gemini(prompt: str, first_model: str | None = None) -> str:
    """Return raw model text. Caller parses file blocks. No key / dry-run -> ''.
    Per-model retries (timeouts / 429 / 5xx, backoff), then falls back to the
    next model in the chain — gemini-3.8-flash has been shedding load with
    503s while other models stay healthy. A model the key can't use (404/400)
    is skipped immediately; auth failure (401/403) aborts. Total failure
    returns '' so the caller takes its existing no-fix path."""
    global LAST_MODEL_USED
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key or os.environ.get("DRY_RUN") == "1":
        return ""
    timeout = float(os.environ.get("GEMINI_TIMEOUT", "300"))
    attempts = int(os.environ.get("GEMINI_ATTEMPTS", "5"))
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": int(os.environ.get("GEMINI_MAX_OUTPUT", "8192"))}}
    if os.environ.get("GEMINI_SERVICE_TIER"):
        body["serviceTier"] = os.environ["GEMINI_SERVICE_TIER"]
    failures = []
    for model in model_chain(first_model):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        last = None
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
                if model != model_chain(first_model)[0]:
                    print(f"Gemini answered via fallback model {model}")
                LAST_MODEL_USED = model
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (401, 403):
                    print(f"Gemini HTTP {e.code} (auth) on {model} — chain aborted"); return ""
                if e.code in (400, 404):
                    print(f"{model} unusable (HTTP {e.code}); moving to next model"); break
                if e.code not in (429, 500, 502, 503, 504):
                    print(f"Gemini HTTP {e.code} on {model} — giving up"); return ""
            except (TimeoutError, urllib.error.URLError, OSError) as e:
                last = e
            if attempt < attempts - 1:
                wait = min(15 * (2 ** attempt), 120)
                print(f"{model} attempt {attempt + 1} failed ({type(last).__name__}: {last}); retrying in {wait}s", flush=True)
                time.sleep(wait)
        failures.append(f"{model}: {type(last).__name__}: {last}")
        print(f"{model} exhausted; trying next model in chain")
    print("All models in fallback chain failed — " + " | ".join(failures))
    return ""

def parse_file_blocks(text: str):
    """Model output format:  FILE: path\\n```\\n...\\n```  blocks."""
    out = []
    for m in re.finditer(r"FILE:\s*(\S+)\s*\n```[^\n]*\n(.*?)```", text or "", re.S):
        path, content = m.group(1).strip(), m.group(2)
        if path.startswith("/") or ".." in path.split("/"):
            continue
        out.append((path, content))
    return out

CONTEXT_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".c", ".h", ".cpp",
                ".go", ".java", ".rb", ".sh", ".yml", ".yaml", ".toml", ".json",
                ".txt", ".md", ".cfg", ".ini")
MAX_CONTEXT_FILES = 8
MAX_CONTEXT_CHARS = 30_000

def extract_paths(text: str):
    """Path-like tokens from logs/issue text: quoted or bare, with optional
    :line suffixes and ./ or /workspace/ prefixes stripped by the caller."""
    out = []
    pat = re.compile(r"[\"']?((?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]{1,5}|[\w.\-]+\.[A-Za-z0-9]{1,5})(?::\d+(?::\d+)?)?[\"']?")
    for m in pat.finditer(text or ""):
        p = m.group(1)
        if p.endswith(CONTEXT_EXTS) and p not in out:
            out.append(p)
    return out

def _norm_candidate(p: str):
    """Map a log path to a repo-relative path that exists, or None."""
    p = p.strip().lstrip("./")
    for marker in ("/workspace/", "/repo/", "repo/"):
        if marker in p: p = p.split(marker, 1)[1]
    if p.startswith("/") or ".." in p.split("/"): return None
    if os.path.isfile(p): return p
    # logs often carry runner paths; try suffix matches against the tree
    parts = p.split("/")
    for i in range(1, len(parts)):
        cand = "/".join(parts[i:])
        if os.path.isfile(cand): return cand
    return None

def gather_context(source_text: str, extra_paths=()):
    """Grounding (Nov 27 2026): the files this failure/issue actually
    implicates — paths named in the logs, plus (failure mode) the files
    changed by the failed commit and dependency manifests. Returns
    {relpath: content} capped so the prompt stays cheap."""
    candidates = []
    for p in list(extra_paths) + extract_paths(source_text):
        rel = _norm_candidate(p)
        if rel and rel not in candidates: candidates.append(rel)
    ctx, total = {}, 0
    for rel in candidates[:MAX_CONTEXT_FILES]:
        try:
            with open(rel, errors="ignore") as fh: content = fh.read()
        except OSError: continue
        content = content[:8000]
        if total + len(content) > MAX_CONTEXT_CHARS: break
        ctx[rel] = content; total += len(content)
    return ctx

def skeleton(rel: str, content: str) -> str:
    """Compact per-file skeleton for triage: imports + definition lines.
    Same builder as repo_improve's — local and free."""
    lines = content.splitlines()
    picked = []
    if rel.endswith(".py"):
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    picked.append(lines[node.lineno - 1].strip())
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    picked.append(lines[node.lineno - 1].strip())
        except SyntaxError:
            picked = []
    if not picked:
        pat = re.compile(r"^\s*(import |from |#include|fn |pub fn |func |function |class |def |export |const \w+ = \(|type \w+|interface )")
        picked = [ln.strip() for ln in lines if pat.match(ln)]
    head = f"{rel} ({len(lines)} lines)"
    body = "\n".join(picked[:40])
    return (head + "\n" + body)[:1200]

_TRIAGE_SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target",
                     "__pycache__", ".venv", "venv", ".next", "out", "coverage"}

def triage_context(source_text: str):
    """Grounding fallback for when nothing is named in the issue/logs:
    skeleton-scan the repo, let a cheap model pick the files this problem
    most likely touches, and ground on their real contents. Returns {}
    when triage finds nothing (caller then uses the blind prompt)."""
    files = []
    for dirpath, dirnames, filenames in os.walk("."):
        dirnames[:] = [d for d in dirnames if d not in _TRIAGE_SKIP_DIRS]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), ".")
            if not rel.endswith(CONTEXT_EXTS): continue
            try: size = os.path.getsize(rel)
            except OSError: continue
            if 0 < size <= 200_000: files.append(rel)
    files.sort()
    if not files: return {}
    skels, total = [], 0
    for rel in files[:80]:
        try:
            with open(rel, errors="ignore") as fh: content = fh.read()
        except OSError: continue
        s = skeleton(rel, content)
        if total + len(s) > 60_000: break
        skels.append(s); total += len(s)
    if not skels: return {}
    prompt = ("A code problem is described below. Below the problem are SKELETONS "
              "(imports + signatures) of the repository's files. Pick the files MOST "
              "likely to contain the cause or the fix. Reply with ONLY one picked path "
              f"per line, at most {MAX_CONTEXT_FILES} paths, no explanations.\n\n"
              "--- PROBLEM ---\n" + source_text[:6000] + "\n\n--- SKELETONS ---\n" + "\n\n".join(skels))
    reply = call_gemini(prompt, first_model=os.environ.get("GEMINI_TRIAGE_MODEL", "gemini-2.5-flash"))
    if not reply: return {}
    picked = []
    for ln in reply.splitlines():
        cand = ln.strip().strip("`*- ")
        rel = _norm_candidate(cand)
        if rel and rel not in picked: picked.append(rel)
    if not picked: return {}
    print(f"  triage picked {len(picked)} file(s) for grounding: {', '.join(picked)}")
    return gather_context("", extra_paths=picked)

def failed_commit_files():
    """Files changed by HEAD (the failed commit in failure mode)."""
    try:
        out = subprocess.run(["git", "show", "--pretty=", "--name-only", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []

def parse_patch_blocks(text: str):
    """PATCH: path blocks with <<<<<<< SEARCH / ======= / >>>>>>> REPLACE."""
    out, order = {}, []
    pat = re.compile(r"PATCH:\s*(\S+)\s*\n<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE", re.S)
    for m in pat.finditer(text or ""):
        path = m.group(1).strip()
        if path.startswith("/") or ".." in path.split("/"): continue
        if path not in out: out[path] = []; order.append(path)
        out[path].append((m.group(2), m.group(3)))
    return [(p, out[p]) for p in order]

def apply_patch(content: str, pairs):
    """Every SEARCH must match exactly once; returns new content or None."""
    for old, new in pairs:
        if content.count(old) != 1: return None
        content = content.replace(old, new)
    return content

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\s+")

def compress_log(text: str) -> str:
    """RTK-style log compression before logs are sent to the model: strip
    ANSI codes and per-line timestamps, collapse consecutive duplicate lines
    (xN), drop blank runs. Failure logs are mostly repetition — this cuts
    input tokens hard without losing the error signal."""
    out, prev, run = [], None, 0
    def flush():
        if run > 1: out.append(f"  ... (previous line repeated x{run})")
    for raw in text.splitlines():
        line = _TS_PREFIX.sub("", _ANSI.sub("", raw)).rstrip()
        if not line:
            continue
        if line == prev:
            run += 1; continue
        flush(); out.append(line); prev, run = line, 1
    flush()
    return "\n".join(out)

def main() -> int:
    mode = os.environ.get("MODE", "")
    repo = os.environ.get("REPO", "")
    if mode not in ("issue", "failure") or not repo:
        print("MODE must be issue|failure and REPO must be set", file=sys.stderr); return 2

    if mode == "issue":
        num = int(os.environ.get("ISSUE_NUMBER", "0") or 0)
        title = os.environ.get("ISSUE_TITLE", "issue fix")
        branch = branch_for_issue(num, title)
        source = f"Issue #{num}: {title}\n\n{os.environ.get('ISSUE_BODY','')}"
    else:
        branch = branch_for_run(os.environ.get("WORKFLOW_NAME","workflow"), os.environ.get("RUN_ID","0"))
        logs = os.environ.get("FAILURE_LOGS", "")
        if is_infra_failure(logs):
            print("Infra failure — skipping, no PR."); return 0
        source = (f"Workflow '{os.environ.get('WORKFLOW_NAME')}' failed. "
                  f"Run: {os.environ.get('RUN_URL','')}\n\nLogs (tail, compressed):\n{compress_log(logs)[-12000:]}")

    if open_pr_exists(repo, branch):
        print(f"Duplicate guard: open PR already exists for {branch}"); return 0

    print(f"mode={mode} repo={repo} branch={branch}")
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN: would ask Gemini for a scoped fix, verify, push branch, open PR.")
        print(f"Source preview: {source[:300]}")
        return 0

    # Grounding: give the model the actual files implicated by the
    # failure/issue instead of asking it to invent whole files from the
    # log tail alone (the pre-grounding failure mode could not produce a
    # reliable fix — its one real attempt generated an unrelated new
    # workflow file). With contents in the prompt, fixes come back as
    # cheap exact patches instead of full-file rewrites.
    extra = []
    if mode == "failure":
        extra += failed_commit_files()
        extra += [m for m in ("package.json", "pyproject.toml", "requirements.txt",
                              "Cargo.toml", "go.mod", "Gemfile") if os.path.isfile(m)]
    ctx = gather_context(source, extra)
    if not ctx:
        # Nothing named in the issue/logs — skeleton-triage the repo so the
        # fix prompt is still grounded in real files instead of blind.
        ctx = triage_context(source)
    ponytail = ("Work like a lazy senior dev: fix the root cause once, reuse what the codebase, "
                "standard library, or platform already provides, no new dependencies or abstractions, "
                "deletion over addition. Never simplify away input validation, error handling, "
                "security, or accessibility. ")
    if ctx:
        print(f"Grounded in {len(ctx)} implicated file(s): {', '.join(ctx)}")
        contents_txt = "".join(f"\n===== {p} =====\n{c}\n" for p, c in ctx.items())
        prompt = (
            "You are a fix bot. The problem is below, followed by the EXACT current contents of the "
            "files it implicates. Produce ONLY patch blocks in this exact format:\n"
            "PATCH: relative/path\n<<<<<<< SEARCH\n<exact original lines, copied verbatim>\n=======\n"
            "<replacement lines>\n>>>>>>> REPLACE\n"
            "One PATCH block per edit. Only for a brand-new file use:\n"
            "FILE: relative/path\n```\n<full new file content>\n```\n"
            "Keep changes minimal and scoped to this problem. " + ponytail + "\n\n"
            + source + "\n\n--- FILE CONTENTS ---" + contents_txt
        )
    else:
        prompt = (
            "You are a fix bot. Produce ONLY file blocks in this exact format for files that must change:\n"
            "FILE: relative/path\n```\n<full new file content>\n```\n"
            "Keep changes minimal and scoped to this problem. " + ponytail + "\n\n" + source
        )
    reply = call_gemini(prompt)
    files = []
    for path, pairs in parse_patch_blocks(reply):
        try:
            with open(path, errors="ignore") as fh: base = fh.read()
        except OSError:
            continue
        applied = apply_patch(base, pairs)
        if applied is None:
            print(f"  patch for {path} did not apply cleanly — skipped")
            continue
        if applied != base: files.append((path, applied))
    patched = {p for p, _ in files}
    for path, content in parse_file_blocks(reply):
        if path not in patched: files.append((path, content))
    if not files:
        print("No parseable fix from model — opening no PR."); return 0
    # The Actions token can never push .github/workflows/* (no workflows
    # permission — the push is rejected outright and used to kill the whole
    # run, losing every other change). Partition those out and report them.
    wf_skipped = [p for p, _ in files if p.startswith(".github/workflows/")]
    files = [(p, c) for p, c in files if not p.startswith(".github/workflows/")]
    if wf_skipped:
        print(f"note: {len(wf_skipped)} workflow file(s) skipped (token cannot push them): {', '.join(wf_skipped)}")
    if not files:
        msg = (f"Auto-PR: the model proposed changes only to workflow files ({', '.join(wf_skipped)}), "
               "which the Actions token cannot push (no `workflows` permission). No PR was opened — "
               "those files need a token with workflows permission, or a manual edit.")
        print(msg)
        if mode == "issue" and os.environ.get("ISSUE_NUMBER"):
            try: gh("POST", f"/repos/{repo}/issues/{os.environ['ISSUE_NUMBER']}/comments", {"body": msg})
            except Exception as e: print(f"  (could not post comment: {e})")
        return 0
    for path, content in files:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f: f.write(content)
    # Verification is handled by the caller workflow step in the full
    # deployment (missing runner = land with warning, real failure = rollback).
    try:
        subprocess.run(["git", "checkout", "-b", branch], check=False)
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "-c", "user.email=autofix@muse.ai", "-c", "user.name=Auto-PR Creator",
                        "commit", "-m", f"autofix: {branch}"], check=True)
        subprocess.run(["git", "push", "origin", branch], check=True)
        kind = f"Fixes #{os.environ.get('ISSUE_NUMBER')}" if mode == "issue" else f"Fixes failed run {os.environ.get('RUN_URL','')}"
        skipped_note = ""
        if wf_skipped:
            skipped_note = ("\n\nSkipped (Actions token cannot push workflow files):\n"
                            + "\n".join(f"- `{p}`" for p in wf_skipped))
        pr = gh("POST", f"/repos/{repo}/pulls", {
            "title": f"[auto-pr] {branch}", "head": branch,
            "base": os.environ.get("BASE_BRANCH", "main"),
            "body": f"Automated fix by Auto-PR Creator.\n\n{kind}{skipped_note}\n\nHuman review required — not auto-merged.",
        })
    except Exception as e:
        # Push/PR failures must not die as a traceback.
        msg = f"Auto-PR generated a fix but could not publish it: {type(e).__name__}: {e}. No PR was opened."
        print(msg)
        if mode == "issue" and os.environ.get("ISSUE_NUMBER"):
            try: gh("POST", f"/repos/{repo}/issues/{os.environ['ISSUE_NUMBER']}/comments", {"body": msg})
            except Exception as ce: print(f"  (could not post comment: {ce})")
        return 0
    print(f"Opened PR: {pr.get('html_url')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
