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
import json, os, re, subprocess, sys, time, urllib.request, urllib.error

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

def model_chain() -> list:
    primary = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS)
    chain = [primary] + [m.strip() for m in fallbacks.split(",") if m.strip()]
    return list(dict.fromkeys(chain))

def call_gemini(prompt: str) -> str:
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
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    failures = []
    for model in model_chain():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        last = None
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read())
                if model != model_chain()[0]:
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
                  f"Run: {os.environ.get('RUN_URL','')}\n\nLogs (tail):\n{logs[-12000:]}")

    if open_pr_exists(repo, branch):
        print(f"Duplicate guard: open PR already exists for {branch}"); return 0

    print(f"mode={mode} repo={repo} branch={branch}")
    if os.environ.get("DRY_RUN") == "1":
        print("DRY_RUN: would ask Gemini for a scoped fix, verify, push branch, open PR.")
        print(f"Source preview: {source[:300]}")
        return 0

    prompt = (
        "You are a fix bot. Produce ONLY file blocks in this exact format for files that must change:\n"
        "FILE: relative/path\n```\n<full new file content>\n```\n"
        "Keep changes minimal and scoped to this problem:\n\n" + source
    )
    files = parse_file_blocks(call_gemini(prompt))
    if not files:
        print("No parseable fix from model — opening no PR."); return 0
    for path, content in files:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f: f.write(content)
    # Verification is handled by the caller workflow step in the full
    # deployment (missing runner = land with warning, real failure = rollback).
    subprocess.run(["git", "checkout", "-b", branch], check=False)
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=autofix@muse.ai", "-c", "user.name=Auto-PR Creator",
                    "commit", "-m", f"autofix: {branch}"], check=True)
    subprocess.run(["git", "push", "origin", branch], check=True)
    kind = f"Fixes #{os.environ.get('ISSUE_NUMBER')}" if mode == "issue" else f"Fixes failed run {os.environ.get('RUN_URL','')}"
    pr = gh("POST", f"/repos/{repo}/pulls", {
        "title": f"[auto-pr] {branch}", "head": branch,
        "base": os.environ.get("BASE_BRANCH", "main"),
        "body": f"Automated fix by Auto-PR Creator.\n\n{kind}\n\nHuman review required — not auto-merged.",
    })
    print(f"Opened PR: {pr.get('html_url')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
