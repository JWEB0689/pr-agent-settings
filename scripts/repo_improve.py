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
  3. Send files to Gemini in batches; ask for concrete improvements only
     (bug risk, security, performance, dead code, readability) returned as
     FILE: blocks with full new content — same format as auto_pr.py.
  4. Apply blocks, cap changed files at MAX_CHANGED_FILES per PR.
  5. Push branch improve/repo-<issue>-<runid> and open a PR summarising
     every change by category. Never pushes to default, never merges.
  6. Dedup: an open improve PR for the same issue/focus blocks a new one.

Env: GH_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, REPO, ISSUE_NUMBER,
     FOCUS (optional path/category, '' = whole repo), DRY_RUN=1 for tests.
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.request

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

def branch_name(issue: str, focus: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (focus or "all").lower()).strip("-")[:20] or "all"
    return f"improve/repo-{issue or 'dispatch'}-{slug}"

class GeminiError(Exception):
    pass

def call_gemini(prompt: str) -> str:
    """One Gemini call with retries. Non-streaming generateContent returns
    everything at once, so a slow model on a big prompt can exceed a plain
    socket timeout (run 46084034282 died at exactly 180s, uncaught). Retry
    timeouts / 429 / 5xx / connection resets with exponential backoff;
    raise GeminiError after the last try so callers can skip a batch
    instead of crashing the whole run. The window is deliberately long:
    run 46086033254 (DeNoiser) burned 3 attempts x 3 batches against a
    Gemini outage (all 503 / connection reset) and still never caught the
    service recovering — 5 attempts with 15/30/60/120s backoff gives one
    batch ~4 minutes of ride-out before the next batch even starts."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key or os.environ.get("DRY_RUN") == "1": return ""
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    timeout = float(os.environ.get("GEMINI_TIMEOUT", "300"))
    attempts = int(os.environ.get("GEMINI_ATTEMPTS", "5"))
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504): raise GeminiError(f"Gemini HTTP {e.code}") from e
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            last = e
        if attempt < attempts - 1:
            wait = min(15 * (2 ** attempt), 120)
            print(f"  Gemini attempt {attempt + 1} failed ({type(last).__name__}: {last}); retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise GeminiError(f"Gemini call failed after {attempts} attempts: {type(last).__name__}: {last}")

def gh(method, path, payload=None):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    req.add_header("Accept", "application/vnd.github+json")
    data = json.dumps(payload).encode() if payload is not None else None
    if data: req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data) as r:
        return json.loads(r.read() or b"{}")

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
    chunks, batch, size = [], [], 0
    for rel in files:
        with open(os.path.join(root, rel), errors="ignore") as fh: content = fh.read()
        if size + len(content) > 30_000 and batch: chunks.append(batch); batch, size = [], 0
        batch.append((rel, content)); size += len(content)
    if batch: chunks.append(batch)
    changed = {}
    failures = []
    for i, batch in enumerate(chunks, 1):
        prompt = ("You are improving an ENTIRE repository, not a PR diff. "
                  f"Focus: {focus or 'all categories (bug risk, security, performance, dead code, readability)'}.\n"
                  "Return ONLY concrete improvements as blocks:\nFILE: relative/path\n```\n<full new file content>\n```\n"
                  "Skip files that need no change. No behaviour changes beyond the improvement.\n\n")
        for rel, content in batch:
            prompt += f"\n===== {rel} =====\n{content}\n"
        try:
            reply = call_gemini(prompt)
        except GeminiError as e:
            print(f"  batch {i}/{len(chunks)} failed: {e}")
            failures.append(str(e)); continue
        for path, content in parse_file_blocks(reply):
            changed[path] = content
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
    # The bot token (GitHub App) can never create or update workflow files —
    # GitHub rejects the push without `workflows` permission (seen on Agent
    # Auto-PR run 46022506220 and DeNoiser improve run 46104464181, where the
    # model's codeql.yml change killed an otherwise-good run). Partition
    # those changes out and report them instead of losing the whole PR.
    wf_changes = {p: c for p, c in changed.items() if p.startswith(".github/workflows/")}
    if wf_changes:
        changed = {p: c for p, c in changed.items() if p not in wf_changes}
        print(f"  note: skipping {len(wf_changes)} workflow-file change(s) the bot cannot push: {sorted(wf_changes)}")
    if not changed and wf_changes:
        msg = ("Repo-wide `/improve`: the model's only proposed change(s) touch workflow files "
               f"({', '.join(sorted(wf_changes))}), which the bot token cannot push "
               "(GitHub requires `workflows` permission for `.github/workflows/`). "
               "No PR was opened — those files need a human push or a workflows-permission token.")
        print(msg)
        if issue:
            try: gh("POST", f"/repos/{repo}/issues/{issue}/comments", {"body": msg})
            except Exception as e: print(f"  (could not post skip comment: {e})")
        return 0
    if not changed:
        print("Model proposed no changes — no PR."); return 0
    for path, content in changed.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        with open(full, "w") as fh: fh.write(content)
    subprocess.run(["git", "checkout", "-b", branch], cwd=root, check=False)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=improve@muse.ai", "-c", "user.name=Repo Improve",
                    "commit", "-m", f"improve: whole-repo improvements ({focus or 'all'})"], cwd=root, check=True)
    try:
        subprocess.run(["git", "push", "origin", branch], cwd=root, check=True)
    except subprocess.CalledProcessError as e:
        msg = (f"Repo-wide `/improve`: the improvement branch `{branch}` could not be pushed "
               f"(git push exited {e.returncode}). No PR was opened. Re-run `/improve` to try again.")
        print(msg)
        if issue:
            try: gh("POST", f"/repos/{repo}/issues/{issue}/comments", {"body": msg})
            except Exception as e2: print(f"  (could not post push-failure comment: {e2})")
        return 1
    listing = "\n".join(f"- `{p}`" for p in sorted(changed))
    skipped_note = ""
    if wf_changes:
        skipped_note = (f"\n\nSkipped — the model also proposed changes to "
                        f"{len(wf_changes)} workflow file(s) the bot token cannot push "
                        f"(`.github/workflows/` needs `workflows` permission): "
                        + ", ".join(f"`{p}`" for p in sorted(wf_changes)))
    pr = gh("POST", f"/repos/{repo}/pulls", {
        "title": f"[improve] Whole-repo improvements ({focus or 'all'})",
        "head": branch, "base": os.environ.get("BASE_BRANCH", "main"),
        "body": (f"Repo-wide `/improve` — scans the entire repo, not just a PR diff.\n\n"
                 f"Focus: {focus or 'all'}\nScanned: {len(files)} files\nChanged: {len(changed)} files\n\n{listing}"
                 f"{skipped_note}\n\n"
                 f"Human review required — not auto-merged."),
    })
    print(f"Opened PR: {pr.get('html_url')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
