#!/usr/bin/env python3
"""
Autonomous fixer for Jeremy's fleet — fixes what the AI code-review bot
(PR-Agent) flags on a PR, and falls back to repairing CI build/test failures.

Modes (decided at runtime):
  1. review-fix — PR-Agent left review findings (inline review comments and/or
     review summary) on the PR. Findings plus the full contents of the flagged
     files go to Gemini, which returns complete updated file contents.
  2. ci-fix — no review findings. Runs the repo's test/check command and asks
     Gemini to repair whatever fails.

Every applied change is re-verified with the repo's test/check command when
one exists; on failure the changes are rolled back. Fixes are committed and
pushed to the PR branch. Never merges — Jeremy still merges.

Env:
  GEMINI_API_KEY   (required) Google AI Studio key
  GEMINI_MODEL     (default: gemini-3.8-flash)
  REPO             owner/repo, for fetching review comments
  PR_NUMBER        PR number (for status comments)
  HEAD_REF         PR head branch (for push)
  BASE_REF         base ref for diff (default: origin/main)
  GH_TOKEN         GitHub token for the gh CLI
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "gemini-3.8-flash"
MAX_FILE_BYTES = 80_000
MAX_CONTEXT_FILES = 12


# ---------------------------------------------------------------- utilities

def run_cmd(cmd, cwd=None, capture=True):
    """Run a shell command, return (exit_code, stdout, stderr)."""
    try:
        res = subprocess.run(
            cmd, cwd=cwd, shell=isinstance(cmd, str),
            text=True, capture_output=capture,
        )
        return res.returncode, res.stdout or "", res.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def get_modified_files(base_ref="origin/main"):
    """Files changed on this branch vs base (fallbacks for shallow checkouts)."""
    code, out, _ = run_cmd(f"git diff --name-only {base_ref}...HEAD")
    if code != 0 or not out.strip():
        code, out, _ = run_cmd("git diff --name-only HEAD~1 HEAD")
    if code != 0 or not out.strip():
        code, out, _ = run_cmd("git status --porcelain")
        files = [line[3:].strip() for line in out.splitlines() if len(line) > 3]
        return [f for f in files if os.path.isfile(f)]
    return [f.strip() for f in out.splitlines() if f.strip() and os.path.isfile(f.strip())]


def detect_test_runner(modified_files):
    """Return (cwd, test_cmd) for this repo layout, or (cwd, None)."""
    if any(f.startswith("v2/") for f in modified_files) and os.path.isfile("v2/Cargo.toml"):
        return "v2", "cargo check --workspace --no-default-features"
    if any(f.startswith("ui/mobile/") for f in modified_files) and os.path.isfile("ui/mobile/package.json"):
        return "ui/mobile", "npm test --if-present"
    if os.path.isfile("Cargo.toml"):
        return ".", "cargo check --workspace"
    if os.path.isfile("package.json"):
        try:
            with open("package.json") as f:
                scripts = json.load(f).get("scripts", {})
                if "test" in scripts:
                    return ".", "npm test"
                if "build" in scripts:
                    return ".", "npm run build"
        except Exception:
            pass
        return ".", "npm test --if-present"
    if os.path.isfile("pyproject.toml") or os.path.isfile("requirements.txt"):
        return ".", "pytest -q"
    if os.path.isfile("go.mod"):
        return ".", "go test ./..."
    return ".", None


def read_file_context(paths):
    """Map of path -> full contents for existing, reasonably-sized files."""
    contexts = {}
    for f in paths:
        if os.path.isfile(f) and os.path.getsize(f) < MAX_FILE_BYTES:
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    contexts[f] = fh.read()
            except Exception:
                pass
        if len(contexts) >= MAX_CONTEXT_FILES:
            break
    return contexts


def call_gemini(api_key, model, prompt):
    """Invoke Gemini, expecting a JSON object per the prompt's schema."""
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
    except urllib.error.HTTPError as e:
        print(f"Gemini API Error {e.code}: {e.read().decode('utf-8', errors='ignore')[:500]}",
              file=sys.stderr)
        return None
    except Exception as e:
        print(f"Gemini request failed: {e}", file=sys.stderr)
        return None


def gh_api_get(path):
    """GET a GitHub REST API path with GH_TOKEN; follows pagination for lists."""
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("GH_TOKEN not set; cannot query GitHub API.", file=sys.stderr)
        return None
    items, page = [], 1
    while True:
        url = f"https://api.github.com{path}?per_page=100&page={page}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
            "User-Agent": "autofix-bot",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"GitHub API error {e.code} for {path}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"GitHub API request failed: {e}", file=sys.stderr)
            return None
        if isinstance(data, list):
            items.extend(data)
            if len(data) < 100:
                break
            page += 1
        else:
            return data
    return items


def post_pr_comment(pr_number, body):
    """Post a status comment on the PR via the REST API."""
    token, repo = os.environ.get("GH_TOKEN"), os.environ.get("REPO")
    if not (token and repo and pr_number):
        return
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    req = urllib.request.Request(
        url, data=json.dumps({"body": body}).encode("utf-8"),
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": "autofix-bot",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception as e:
        print(f"comment failed: {e}", file=sys.stderr)


def safe_normpath(fpath):
    """Normalized relative path, or None if it escapes the repo."""
    norm = os.path.normpath(fpath)
    if norm.startswith("..") or os.path.isabs(norm):
        return None
    return norm


# ------------------------------------------------------- review findings

def get_review_findings(repo, pr_number):
    """Collect review findings: inline review comments + review summaries."""
    findings = []
    if not repo or not pr_number:
        return findings

    for c in gh_api_get(f"/repos/{repo}/pulls/{pr_number}/comments") or []:
        body = (c.get("body") or "").strip()
        if not body or body.strip() == "/autofix":
            continue
        findings.append({
            "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"),
            "author": (c.get("user") or {}).get("login"),
            "body": body[:2000],
        })

    # Review summaries posted as issue comments (PR-Agent posts one).
    summaries = []
    for c in gh_api_get(f"/repos/{repo}/issues/{pr_number}/comments") or []:
        body = (c.get("body") or "").strip()
        author = (c.get("user") or {}).get("login")
        if not body or body.strip() == "/autofix":
            continue
        if author in ("github-actions[bot]",) and len(body) > 200:
            summaries.append(body[:3000])
    for s in summaries[-2:]:
        findings.append({"path": None, "line": None,
                         "author": "review-summary", "body": s})

    return findings


REVIEW_SYSTEM_PROMPT = """You are an autonomous senior software engineer fixing code review findings.
An AI code reviewer left the findings below on a pull request. For each finding:

1. Fix genuine bugs, errors, security issues, and broken logic the reviewer flagged.
2. Skip open questions, requests for discussion, and pure style nitpicks unless the fix is trivial and clearly safe.
3. Keep changes minimal, surgical, and idiomatic to the codebase.

Respond ONLY with a JSON object conforming strictly to this schema:
{
  "summary": "Short explanation of what was fixed",
  "files": [
    {"path": "relative/path/to/file", "content": "Full, complete updated file contents"}
  ]
}
Do not use placeholders or omit lines. If no finding warrants a code change, return {"summary": "no changes needed", "files": []}.
"""

CI_SYSTEM_PROMPT = """You are an autonomous senior software engineer. The project's build/test check failed with the error below.
1. Diagnose the exact root cause of the error.
2. Provide the complete updated content for each file that needs to be fixed. Do not use placeholders or omit lines.
3. Keep changes minimal, surgical, and idiomatic.

Respond ONLY with a JSON object conforming strictly to this schema:
{
  "summary": "Short explanation of root cause and fix applied",
  "files": [
    {"path": "relative/path/to/file", "content": "Full, complete updated file contents"}
  ]
}
"""


def apply_repair_files(repair_result, allowed_paths):
    """Write Gemini's file updates (guarded). Returns (summary, [paths])."""
    if not repair_result or "files" not in repair_result or not repair_result["files"]:
        return None, []
    summary = repair_result.get("summary", "Applied code repair")
    repaired = []
    for item in repair_result["files"]:
        fpath, fcontent = item.get("path"), item.get("content")
        if not fpath or fcontent is None:
            continue
        norm = safe_normpath(fpath)
        if norm is None:
            print(f"Warning: rejected insecure path: {fpath}")
            continue
        if allowed_paths is not None and norm not in allowed_paths:
            print(f"Warning: rejected out-of-scope path: {norm}")
            continue
        parent = os.path.dirname(norm)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(norm, "w", encoding="utf-8") as out_f:
            out_f.write(fcontent)
        repaired.append(norm)
        print(f"  Updated: {norm}")
    if not repaired:
        return None, []
    return summary, repaired


def verify_commit_push(summary, repaired_files, test_cwd, test_cmd,
                       pr_number, head_ref, verify_label):
    """Re-verify, commit, push, and comment. Returns True on success."""
    if test_cmd:
        print(f"==> Re-running validation: '{test_cmd}' in '{test_cwd}'")
        v_code, v_out, v_err = run_cmd(test_cmd, cwd=test_cwd)
        if v_code != 0:
            print("Re-verification failed after applying fix. Rolling back.")
            run_cmd("git checkout -- .")
            if pr_number:
                post_pr_comment(pr_number,
                    f"Autofix verification failed: applied `{summary}`, but "
                    f"`{test_cmd}` still failed. Changes were rolled back.\n"
                    f"```\n{(v_out + v_err)[-300:]}\n```")
            return False
        verify_note = f"`{test_cmd}` passed"
    else:
        verify_note = "no local test command; CI will verify"

    print("Re-verification passed (or skipped). Committing...")
    run_cmd(["git", "config", "user.name", "github-actions[bot]"])
    run_cmd(["git", "config", "user.email",
             "41898282+github-actions[bot]@users.noreply.github.com"])
    run_cmd(["git", "add"] + repaired_files)
    c_code, _, _ = run_cmd(["git", "commit", "-m",
                            f"fix(autofix): {summary}\n\nAutonomous repair. {verify_label}"])
    if c_code != 0:
        print("No changes to commit or commit failed.")
        return True  # nothing to do; not a failure

    _, sha, _ = run_cmd(["git", "rev-parse", "--short", "HEAD"])
    sha = sha.strip()

    if head_ref:
        print(f"==> Pushing commit {sha} to origin {head_ref}...")
        p_code, _, p_err = run_cmd(["git", "push", "origin", f"HEAD:{head_ref}"])
        if p_code != 0:
            print(f"Push failed: {p_err}", file=sys.stderr)
            return False
        print("Pushed successfully!")

    if pr_number:
        files_md = "\n".join(f"- `{f}`" for f in repaired_files)
        post_pr_comment(pr_number,
            f"Autonomous AI repair applied\n\n**Summary:** {summary}\n\n"
            f"**Files repaired:**\n{files_md}\n\n"
            f"**Verification:** {verify_note}\n**Commit:** `{sha}`")
    return True


# ---------------------------------------------------------------- modes

def review_fix_mode(api_key, model, repo, pr_number, head_ref, base_ref):
    """Fix what the review bot flagged. Returns True if it handled the PR."""
    findings = get_review_findings(repo, pr_number)
    print(f"==> Found {len(findings)} review finding(s).")
    if not findings:
        return False

    flagged = {f["path"] for f in findings if f.get("path")}
    modified = set(get_modified_files(base_ref))
    allowed = flagged | modified
    print(f"==> Flagged files: {sorted(flagged)[:10]}")

    contexts = read_file_context(sorted(flagged | set(list(modified)[:8])))
    if not contexts:
        print("No file context available; skipping review-fix.")
        return False

    findings_text = json.dumps(
        [{"path": f["path"], "line": f["line"], "body": f["body"]}
         for f in findings], indent=2)[:8000]
    prompt = (REVIEW_SYSTEM_PROMPT
              + f"\n--- REVIEW FINDINGS (PR #{pr_number}) ---\n{findings_text}\n--- END FINDINGS ---\n"
              + f"\n--- FILE CONTENTS ---\n{json.dumps(contexts, indent=2)}\n--- END FILES ---\n"
              + "\nOnly modify files listed in the findings or already changed in this PR.\n")

    print("==> Querying Gemini for review fixes...")
    result = call_gemini(api_key, model, prompt)
    summary, repaired = apply_repair_files(result, allowed)
    if not repaired:
        print("Gemini returned no applicable fixes.")
        if pr_number:
            post_pr_comment(pr_number,
                "Autofix reviewed the AI reviewer's findings and determined no code changes were warranted.")
        return True  # handled: nothing to fix

    print(f"==> Applying review fixes: {summary}")
    test_cwd, test_cmd = detect_test_runner(list(modified))
    ok = verify_commit_push(summary, repaired, test_cwd, test_cmd,
                            pr_number, head_ref, "review-findings repair")
    if not ok:
        sys.exit(1)
    return True


def extract_relevant_files_from_error(error_log):
    """File paths mentioned in compiler/runtime errors."""
    extracted = set()
    for m in re.finditer(r"-->\s+([a-zA-Z0-9_\-/\.]+\.\w+):(\d+)", error_log):
        if os.path.isfile(m.group(1).strip()):
            extracted.add(m.group(1).strip())
    for m in re.finditer(r"([a-zA-Z0-9_\-/\.]+\.(?:rs|ts|tsx|js|jsx|py|go|toml|json)):(\d+)", error_log):
        if os.path.isfile(m.group(1).strip()):
            extracted.add(m.group(1).strip())
    return list(extracted)


def ci_fix_mode(api_key, model, pr_number, head_ref, base_ref):
    """Repair CI build/test failures. Returns True on success."""
    modified_files = get_modified_files(base_ref)
    print(f"==> Detected {len(modified_files)} modified file(s).")
    test_cwd, test_cmd = detect_test_runner(modified_files)
    if not test_cmd:
        print("==> No standard test/check command detected. Exiting gracefully.")
        return True

    print(f"==> Running validation: '{test_cmd}' in '{test_cwd}'")
    code, stdout, stderr = run_cmd(test_cmd, cwd=test_cwd)
    if code == 0:
        print("Validation succeeded — build and tests are passing!")
        if pr_number:
            post_pr_comment(pr_number,
                f"Autonomous verification: `{test_cmd}` passed with 0 errors. No code repairs required.")
        return True

    print(f"Validation failed with exit code {code}.")
    combined = (stdout + "\n" + stderr).strip()
    lines = combined.splitlines()
    error_snippet = "\n".join(lines[-120:]) if len(lines) > 120 else combined

    target_files = set(extract_relevant_files_from_error(combined))
    for f in modified_files[:8]:
        target_files.add(f)
    contexts = read_file_context(sorted(target_files))
    allowed = set(contexts) | set(modified_files)

    prompt = (CI_SYSTEM_PROMPT
              + f"\n--- TEST / BUILD ERROR ---\n{error_snippet}\n--- END ERROR ---\n"
              + f"\n--- FILE CONTENTS ---\n{json.dumps(contexts, indent=2)}\n--- END FILES ---\n")
    print("==> Querying Gemini for CI repair...")
    result = call_gemini(api_key, model, prompt)
    summary, repaired = apply_repair_files(result, allowed)
    if not repaired:
        print("Gemini did not return a valid repair patch.", file=sys.stderr)
        if pr_number:
            post_pr_comment(pr_number,
                "Autofix failed: Gemini was unable to generate a valid repair patch for:\n"
                f"```\n{error_snippet[:500]}\n```")
        return False

    print(f"==> Applying CI repair: {summary}")
    return verify_commit_push(summary, repaired, test_cwd, test_cmd,
                              pr_number, head_ref, "CI-failure repair")


# ---------------------------------------------------------------- main

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    repo = os.environ.get("REPO")
    pr_number = os.environ.get("PR_NUMBER")
    head_ref = os.environ.get("HEAD_REF")
    base_ref = os.environ.get("BASE_REF", "origin/main")

    print(f"==> Starting autonomous fixer with model: {model} (PR #{pr_number})")

    # Mode 1: fix what the review bot flagged.
    try:
        if review_fix_mode(api_key, model, repo, pr_number, head_ref, base_ref):
            return 0
    except Exception as e:
        print(f"review-fix mode errored ({e}); falling back to CI-fix mode.",
              file=sys.stderr)

    # Mode 2: repair CI build/test failures.
    ok = ci_fix_mode(api_key, model, pr_number, head_ref, base_ref)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
