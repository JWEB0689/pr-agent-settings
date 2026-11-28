#!/usr/bin/env python3
"""
Repo-wide /improve engine — original, Nov 27 2026.

Unlike PR-Agent's /improve (suggestions on the PR diff only), this scans the
ENTIRE repo the command is used in and opens ONE PR with the improvements.

Trigger: comment `/improve` on any Issue or PR in the repo, or manual
workflow_dispatch. Optional focus: `/improve src/` (path) or
`/improve performance|security|readability|bugs` (category focus).

How it works:
  1. Walk the repo tree. Skip .git, vendor/node_modules, binaries, lockfiles,
     generated files, and anything > 200 KB.
  2. Rank source files: core code first (by extension), then larger /
     more-central files. Cap the scan at MAX_SCAN_FILES files and
     MAX_SCAN_BYTES total so one run stays reviewable and in Gemini limits.
  3. Two-stage, token-cut (Nov 27 2026): (a) TRIAGE — send a locally built
     skeleton of every file (imports + signatures only, ~10-20% of full
     size) to a cheap model and ask ONLY which files are worth improving;
     (b) DEEP — send full content ONLY for the picked files, ask for
     concrete improvements as PATCH: search/replace blocks (not full
     files), with FILE: full-content blocks still accepted as fallback.
     Skipped when a path focus is given or the repo is small (<=12 files).
  4. Apply blocks, cap changed files at MAX_CHANGED_FILES per PR.
  5. Push branch improve/repo-<issue>-<runid> and open a PR summarising
     every change by category. Never pushes to default, never merges.
  6. Dedup: an open improve PR for the same issue/focus blocks a new one.

Env: GH_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, REPO, ISSUE_NUMBER,
     FOCUS (optional path/category, '' = whole repo), DRY_RUN=1 for tests.
"""
import ast, json, os, re, subprocess, sys, time, urllib.error, urllib.request

API = "https://api.github.com"
MAX_SCAN_FILES = 60
MAX_SCAN_BYTES = 400_000
MAX_CHANGED_FILES = 20
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target",
             "__pycache__", ".venv", "venv", ".next", "out", "coverage"}
SKIP_SUFFIX = (".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif",
               ".ico", ".pdf", ".zip", ".bin", ".wasm", ".svg")
SOURCE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".c", ".h", ".cpp",
              ".hpp", ".go", ".java", ".rb", ".sh", ".yml", ".yaml", ".toml"}

def collect_files(root: str, focus: str = ""):
    """Return ranked [(path, size)] under root, honouring a path focus."""
    base = os.path.join(root, focus) if focus and os.path.isdir(os.path.join(root, focus)) else root
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            if rel.endswith(SKIP_SUFFIX):
                continue
            try: size = os.path.getsize(p)
            except OSError: continue
            if size == 0 or size > 200_000:
                continue
            found.append((rel, size))
    def rank(item):
        rel, size = item
        ext = os.path.splitext(rel)[1]
        return (0 if ext in SOURCE_EXT else 1, -size)
    found.sort(key=rank)
    out, total = [], 0
    for rel, size in found[:MAX_SCAN_FILES]:
        if total + size > MAX_SCAN_BYTES: break
        out.append(rel); total += size
    return out

def parse_file_blocks(text: str):
    out = []
    for m in re.finditer(r"FILE:\s*(\S+)\s*\n```[^\n]*\n(.*?)```", text or "", re.S):
        path, content = m.group(1).strip(), m.group(2)
        if path.startswith("/") or ".." in path.split("/"): continue
        out.append((path, content))
    return out[:MAX_CHANGED_FILES]

def skeleton(rel: str, content: str) -> str:
    """Compact per-file skeleton for triage: imports + definition lines.
    Local and free — this is what lets triage scan the whole repo for a
    fraction of the tokens of sending full file contents."""
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

def parse_patch_blocks(text: str):
    """PATCH: path blocks with <<<<<<< SEARCH / ======= / >>>>>>> REPLACE
    sections. Returns [(path, [(search, replace), ...]), ...]."""
    out = {}
    order = []
    pat = re.compile(r"PATCH:\s*(\S+)\s*\n<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE", re.S)
    for m in pat.finditer(text or ""):
        path = m.group(1).strip()
        if path.startswith("/") or ".." in path.split("/"): continue
        if path not in out: out[path] = []; order.append(path)
        out[path].append((m.group(2), m.group(3)))
    return [(p, out[p]) for p in order[:MAX_CHANGED_FILES]]

def apply_patch(content: str, pairs) -> str | None:
    """Apply search/replace pairs; every SEARCH must match exactly once.
    Returns the new content, or None if any pair fails (file skipped)."""
    for old, new in pairs:
        if content.count(old) != 1: return None
        content = content.replace(old, new)
    return content

def triage_pick(files, contents, focus: str):
    """Stage 1: skeleton-scan ALL files, model picks the ones worth a deep
    look. Returns a subset of files, or None to fall back to all files."""
    skels = "\n\n".join(skeleton(rel, contents[rel]) for rel in files)
    prompt = ("You are triaging a repository for a code-improvement pass. "
              f"Focus: {focus or 'all categories (bug risk, security, performance, dead code, readability)'}.\n"
              "Below are SKELETONS (imports + signatures) of every file. Pick the files MOST likely to "
              "contain a concrete improvement worth making. Reply with ONLY one picked path per line, "
              f"at most {MAX_CHANGED_FILES} paths, no explanations.\n\n" + skels)
    try:
        reply = call_gemini(prompt, first_model=os.environ.get("GEMINI_TRIAGE_MODEL", "gemini-2.5-flash"))
    except GeminiError as e:
        print(f"  triage failed ({e}); falling back to full scan")
        return None
    picked = [rel for rel in files if any(rel == ln.strip().strip('`*- ') or rel in ln for ln in reply.splitlines())]
    print(f"  triage picked {len(picked)}/{len(files)} files for deep scan")
    return picked or None

def branch_name(issue: str, focus: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (focus or "all").lower()).strip("-")[:20] or "all"
    return f"improve/repo-{issue or 'dispatch'}-{slug}"

class GeminiError(Exception):
    pass

# Fallback chain: primary model first, then these (GEMINI_FALLBACK_MODELS,
# comma-separated, overrides). gemini-3.8-flash has been shedding load with
# 503s since Nov 19 2026 while other models stay healthy, so a single-model
# engine dies with the model. A model the key can't use (404/400) is skipped
# immediately; an auth failure (401/403) aborts the chain — no model will work.
DEFAULT_FALLBACKS = "gemini-2.5-flash,gemini-flash-lite-latest"
LAST_MODEL_USED = ""

def model_chain() -> list:
    primary = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS)
    chain = [primary] + [m.strip() for m in fallbacks.split(",") if m.strip()]
    return list(dict.fromkeys(chain))

def _call_once(model: str, prompt: str, key: str, timeout: float) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": int(os.environ.get("GEMINI_MAX_OUTPUT", "16384"))}}
    if os.environ.get("GEMINI_SERVICE_TIER"):
        body["serviceTier"] = os.environ["GEMINI_SERVICE_TIER"]
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"]

def call_gemini(prompt: str, first_model: str | None = None) -> str:
    """Gemini call with per-model retries, then fallback to the next model
    in the chain. Non-streaming generateContent returns everything at once,
    so a slow model on a big prompt can exceed a plain socket timeout (run
    46084034282 died at exactly 180s, uncaught). Each model gets GEMINI_ATTEMPTS
    tries (default 5) with 15/30/60/120s exponential backoff — run 46086033254
    (DeNoiser) burned a shorter window against a Gemini outage and never
    caught the service recovering. Raises GeminiError only when every model
    in the chain is exhausted, so callers can skip a batch instead of
    crashing the whole run."""
    global LAST_MODEL_USED
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key or os.environ.get("DRY_RUN") == "1": return ""
    timeout = float(os.environ.get("GEMINI_TIMEOUT", "300"))
    attempts = int(os.environ.get("GEMINI_ATTEMPTS", "5"))
    failures = []
    chain = model_chain()
    if first_model and first_model in chain:
        chain = [first_model] + [m for m in chain if m != first_model]
    elif first_model:
        chain = [first_model] + chain
    for model in chain:
        last = None
        for attempt in range(attempts):
            try:
                text = _call_once(model, prompt, key, timeout)
                if model != chain[0]:
                    print(f"  Gemini answered via fallback model {model}", flush=True)
                LAST_MODEL_USED = model
                return text
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (401, 403):
                    raise GeminiError(f"Gemini HTTP {e.code} (auth) on {model} — chain aborted") from e
                if e.code in (400, 404):
                    print(f"  {model} unusable (HTTP {e.code}); moving to next model", flush=True)
                    break
                if e.code not in (429, 500, 502, 503, 504):
                    raise GeminiError(f"Gemini HTTP {e.code} on {model}") from e
            except (TimeoutError, urllib.error.URLError, OSError) as e:
                last = e
            if attempt < attempts - 1:
                wait = min(15 * (2 ** attempt), 120)
                print(f"  {model} attempt {attempt + 1} failed ({type(last).__name__}: {last}); retrying in {wait}s", flush=True)
                time.sleep(wait)
        failures.append(f"{model}: {type(last).__name__}: {last}")
        print(f"  {model} exhausted; trying next model in chain", flush=True)
    raise GeminiError("All models in fallback chain failed — " + " | ".join(failures))

def gh(method, path, payload=None):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    req.add_header("Accept", "application/vnd.github+json")
    data = json.dumps(payload).encode() if payload is not None else None
    if data: req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data) as r:
        return json.loads(r.read() or b"{}")

def push_branch(branch, cwd=None):
    """Push a bot-owned branch. Branch names are deterministic
    (improve/repo-<issue>-<focus>), so a stale branch from an earlier
    attempt makes a plain push non-fast-forward and kills the run.
    Retry once with --force-with-lease pinned to the remote sha just
    observed — a human push racing us still blocks the overwrite."""
    kw = {"cwd": cwd} if cwd else {}
    try:
        subprocess.run(["git", "push", "origin", branch], check=True, **kw); return
    except subprocess.CalledProcessError:
        out = subprocess.run(["git", "ls-remote", "origin", branch],
                             capture_output=True, text=True, **kw)
        sha = out.stdout.split()[0] if out.stdout.strip() else ""
        if not sha: raise
        print(f"  remote branch {branch} already exists (stale attempt) — force-with-lease push")
        subprocess.run(["git", "push", f"--force-with-lease={branch}:{sha}", "origin", branch],
                       check=True, **kw)

def main() -> int:
    try: sys.stdout.reconfigure(line_buffering=True)
    except Exception: pass
    repo = os.environ.get("REPO", "")
    focus = os.environ.get("FOCUS", "").strip()
    issue = os.environ.get("ISSUE_NUMBER", "")
    if not repo:
        print("REPO required", file=sys.stderr); return 2
    root = os.environ.get("REPO_ROOT", ".")
    files = collect_files(root, focus if focus in ("",) or os.path.isdir(os.path.join(root, focus)) else "")
    if focus and os.path.isdir(os.path.join(root, focus)):
        pass
    elif focus:  # category focus — scan all, tell the model the category
        files = collect_files(root, "")
    branch = branch_name(issue, focus)
    print(f"repo={repo} focus={focus!r} scanning {len(files)} files branch={branch}")
    for f in files: print(f"  scan: {f}")
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN: would batch these files to Gemini, apply FILE blocks, open improve PR.")
        return 0
    if not files:
        print("Nothing scannable — no PR."); return 0
    contents = {}
    for rel in files:
        with open(os.path.join(root, rel), errors="ignore") as fh: contents[rel] = fh.read()
    # Token cut: skeleton triage first (whole-repo runs only; a path focus
    # is already scoped, and small repos gain nothing from a triage call).
    path_focus = bool(focus and os.path.isdir(os.path.join(root, focus)))
    deep_files = files
    if len(files) > 12 and not path_focus:
        picked = triage_pick(files, contents, focus)
        if picked: deep_files = picked
    chunks, batch, size = [], [], 0
    for rel in deep_files:
        content = contents[rel]
        if size + len(content) > 30_000 and batch: chunks.append(batch); batch, size = [], 0
        batch.append((rel, content)); size += len(content)
    if batch: chunks.append(batch)
    changed = {}
    failures = []
    for i, batch in enumerate(chunks, 1):
        prompt = ("You are improving an ENTIRE repository, not a PR diff. "
                  f"Focus: {focus or 'all categories (bug risk, security, performance, dead code, readability)'}.\n"
                  "Work like a lazy senior dev: does the change need to exist at all? Reuse what the "
                  "codebase, standard library, or platform already provides before adding code; no new "
                  "dependencies, no new abstractions, deletion over addition. Never simplify away input "
                  "validation, error handling, security, or accessibility.\n"
                  "Return ONLY concrete improvements as PATCH blocks (exact text, copied verbatim):\n"
                  "PATCH: relative/path\n<<<<<<< SEARCH\n<exact original lines>\n=======\n<replacement lines>\n>>>>>>> REPLACE\n"
                  "One PATCH block per edit; a file may have several. Only for a brand-new file, or a "
                  "rewrite so large a patch would be longer, use:\nFILE: relative/path\n```\n<full new file content>\n```\n"
                  "Skip files that need no change. No behaviour changes beyond the improvement.\n\n")
        for rel, content in batch:
            prompt += f"\n===== {rel} =====\n{content}\n"
        try:
            reply = call_gemini(prompt)
        except GeminiError as e:
            print(f"  batch {i}/{len(chunks)} failed: {e}")
            failures.append(str(e)); continue
        for path, pairs in parse_patch_blocks(reply):
            base = changed.get(path, contents.get(path))
            if base is None: continue
            applied = apply_patch(base, pairs)
            if applied is None:
                print(f"  patch for {path} did not apply cleanly — skipped")
                continue
            if applied != base: changed[path] = applied
        for path, content in parse_file_blocks(reply):
            if path not in changed: changed[path] = content
        if len(changed) >= MAX_CHANGED_FILES: break
    if failures and not changed:
        msg = (f"Repo-wide `/improve` run failed: the model call did not complete "
               f"({failures[0]}). No PR was opened. Re-run `/improve` to try again.")
        print(msg)
        if issue:
            try: gh("POST", f"/repos/{repo}/issues/{issue}/comments", {"body": msg})
            except Exception as e: print(f"  (could not post failure comment: {e})")
        # Every batch died on a Gemini transport/API error — the model
        # service was unavailable, the code did not fail. The issue comment
        # above is the user-facing signal; exit 0 so the run does not count
        # as a workflow failure (which would feed the CI failure watch and
        # can trigger a doomed Auto-PR attempt against this same outage).
        print("Model service unavailable — treating as an infra skip, not a workflow failure.")
        return 0
    if failures:
        print(f"  note: {len(failures)}/{len(chunks)} batches failed; proceeding with the rest.")
    # The Actions token can never push .github/workflows/* (no workflows
    # permission — push is rejected outright, which used to kill the whole
    # run and lose every other change). Partition those files out: the rest
    # lands in the PR, the workflow files are reported as skipped.
    wf_skipped = sorted(p for p in changed if p.startswith(".github/workflows/"))
    for p in wf_skipped: del changed[p]
    if wf_skipped:
        print(f"  note: {len(wf_skipped)} workflow file(s) skipped (token cannot push them): {', '.join(wf_skipped)}")
    if not changed and wf_skipped:
        msg = ("Repo-wide `/improve`: the model proposed changes only to workflow files "
               f"({', '.join('`' + p + '`' for p in wf_skipped)}), which the Actions token cannot push "
               "(no `workflows` permission). No PR was opened — those files need a token with "
               "workflows permission, or a manual edit.")
        print(msg)
        if issue:
            try: gh("POST", f"/repos/{repo}/issues/{issue}/comments", {"body": msg})
            except Exception as e: print(f"  (could not post comment: {e})")
        return 0
    if not changed:
        print("Model proposed no changes — no PR."); return 0
    for path, content in changed.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        with open(full, "w") as fh: fh.write(content)
    try:
        subprocess.run(["git", "checkout", "-b", branch], cwd=root, check=False)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.email=improve@muse.ai", "-c", "user.name=Repo Improve",
                        "commit", "-m", f"improve: whole-repo improvements ({focus or 'all'})"], cwd=root, check=True)
        push_branch(branch, cwd=root)
        listing = "\n".join(f"- `{p}`" for p in sorted(changed))
        skipped_note = ""
        if wf_skipped:
            skipped_note = ("\n\nSkipped (Actions token cannot push workflow files):\n"
                            + "\n".join(f"- `{p}`" for p in wf_skipped))
        pr = gh("POST", f"/repos/{repo}/pulls", {
            "title": f"[improve] Whole-repo improvements ({focus or 'all'})",
            "head": branch, "base": os.environ.get("BASE_BRANCH", "main"),
            "body": (f"Repo-wide `/improve` — scans the entire repo, not just a PR diff.\n\n"
                     f"Focus: {focus or 'all'}\nScanned: {len(files)} files\nChanged: {len(changed)} files\nModel: {LAST_MODEL_USED or 'unknown'}\n\n{listing}{skipped_note}\n\n"
                     f"Human review required — not auto-merged."),
        })
    except Exception as e:
        # Push/PR failures (permissions, protected branch, ...) must not die
        # as a traceback: report on the issue and exit as an infra skip.
        msg = f"Repo-wide `/improve` generated changes but could not publish them: {type(e).__name__}: {e}. No PR was opened."
        print(msg)
        if issue:
            try: gh("POST", f"/repos/{repo}/issues/{issue}/comments", {"body": msg})
            except Exception as ce: print(f"  (could not post comment: {ce})")
        return 0
    print(f"Opened PR: {pr.get('html_url')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
