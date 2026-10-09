#!/usr/bin/env python3
"""
Autonomous CI Auto-Fixer Engine powered by Google Gemini.
Diagnoses test/build errors, queries Gemini for structured code repairs,
verifies them against the local test suite, and commits/pushes cleanly.
"""

import os
import sys
import json
import re
import subprocess
from pathlib import Path
import urllib.request
import urllib.error

def run_cmd(cmd, cwd=None, capture=True, check=False):
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        res = subprocess.run(
            cmd,
            cwd=cwd,
            shell=isinstance(cmd, str),
            text=True,
            capture_output=capture,
            check=check
        )
        return res.returncode, res.stdout or "", res.stderr or ""
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout or "", e.stderr or ""
    except Exception as e:
        return 1, "", str(e)

def get_modified_files(base_ref="origin/main"):
    """Get list of modified files on the current branch compared to base."""
    code, out, _ = run_cmd(f"git diff --name-only {base_ref}...HEAD")
    if code != 0 or not out.strip():
        code, out, _ = run_cmd("git diff --name-only HEAD~1 HEAD")
    if code != 0 or not out.strip():
        code, out, _ = run_cmd("git status --porcelain")
        files = [line[3:].strip() for line in out.splitlines() if len(line) > 3]
        return [f for f in files if os.path.isfile(f)]
    return [f.strip() for f in out.splitlines() if f.strip() and os.path.isfile(f.strip())]

def detect_test_runner(modified_files):
    """Determine the cwd and test/check command based on repository layout and modified files."""
    # Check for monorepo subdirectories first
    if any(f.startswith("v2/") for f in modified_files) and os.path.isfile("v2/Cargo.toml"):
        return "v2", "cargo check --workspace --no-default-features"
    if any(f.startswith("ui/mobile/") for f in modified_files) and os.path.isfile("ui/mobile/package.json"):
        return "ui/mobile", "npm test --if-present"
    if any(f.startswith("v2/crates/wifi-densepose-desktop/ui/") for f in modified_files) and os.path.isfile("v2/crates/wifi-densepose-desktop/ui/package.json"):
        return "v2/crates/wifi-densepose-desktop/ui", "npm test --if-present"

    # Top-level checks
    if os.path.isfile("Cargo.toml"):
        return ".", "cargo check --workspace"
    if os.path.isfile("package.json"):
        try:
            with open("package.json") as f:
                data = json.load(f)
                scripts = data.get("scripts", {})
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

def extract_relevant_files_from_error(error_log):
    """Scan error logs for file paths mentioned in compiler or runtime errors."""
    extracted = set()
    # Rust error patterns: --> crates/.../src/lib.rs:10:5
    for m in re.finditer(r"-->\s+([a-zA-Z0-9_\-/\.]+):(\d+)", error_log):
        path = m.group(1).strip()
        if os.path.isfile(path):
            extracted.add(path)
    # Generic trace / node / python: ... path/to/file.ext:line
    for m in re.finditer(r"([a-zA-Z0-9_\-/\.]+\.(?:rs|ts|tsx|js|jsx|py|go|toml|json)):(\d+)", error_log):
        path = m.group(1).strip()
        if os.path.isfile(path):
            extracted.add(path)
    return list(extracted)

def call_gemini(api_key, model, prompt):
    """Invoke Google Gemini API with structured JSON output expectation."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json"
        }
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"Gemini API Error {e.code}: {err_body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Gemini Request failed: {e}", file=sys.stderr)
        return None

def post_pr_comment(pr_number, body):
    """Post feedback comment to PR using gh CLI if available."""
    if not pr_number:
        return
    run_cmd(f"gh pr comment {pr_number} --body '{body}'")

def main():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_STUDIO_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    pr_number = os.environ.get("PR_NUMBER")
    base_ref = os.environ.get("BASE_REF", "origin/main")

    print(f"==> Starting Autonomous Auto-Fixer with model: {model}")
    modified_files = get_modified_files(base_ref)
    print(f"==> Detected {len(modified_files)} modified file(s): {modified_files[:10]}")

    test_cwd, test_cmd = detect_test_runner(modified_files)
    if not test_cmd:
        print("==> No standard test/check command detected. Exiting gracefully.")
        sys.exit(0)

    print(f"==> Running validation check: '{test_cmd}' in '{test_cwd}'")
    code, stdout, stderr = run_cmd(test_cmd, cwd=test_cwd)

    if code == 0:
        print("✅ Validation succeeded. Build and tests are passing!")
        if pr_number:
            post_pr_comment(pr_number, f"✅ **Autonomous Verification**: `{test_cmd}` passed with 0 errors. No code repairs required.")
        sys.exit(0)

    print(f"❌ Validation failed with exit code {code}.")
    combined_log = (stdout + "\n" + stderr).strip()
    # Truncate to relevant tail if massive
    log_lines = combined_log.splitlines()
    error_snippet = "\n".join(log_lines[-120:]) if len(log_lines) > 120 else combined_log

    # Find relevant source files
    target_files = set(extract_relevant_files_from_error(combined_log))
    for f in modified_files[:8]:
        target_files.add(f)

    file_contexts = {}
    for f in target_files:
        if os.path.isfile(f) and os.path.getsize(f) < 80_000:
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    file_contexts[f] = fh.read()
            except Exception:
                pass

    print(f"==> Assembled context for {len(file_contexts)} file(s). Querying Gemini...")

    prompt = f"""
You are an autonomous senior software engineer. The project's build/test check failed with the following error:

--- TEST / BUILD ERROR ---
{error_snippet}
--- END ERROR ---

Here are the contents of the relevant files currently in the repository:
{json.dumps(file_contexts, indent=2)}

TASK:
1. Diagnose the exact root cause of the error.
2. Provide the complete updated content for each file that needs to be fixed. Do not use placeholders or omit lines.
3. Keep changes minimal, surgical, and idiomatic.

Respond ONLY with a JSON object conforming strictly to this JSON schema:
{{
  "summary": "Short explanation of root cause and fix applied",
  "files": [
    {{
      "path": "relative/path/to/file",
      "content": "Full, complete updated file contents"
    }}
  ]
}}
"""

    repair_result = call_gemini(api_key, model, prompt)
    if not repair_result or "files" not in repair_result or not repair_result["files"]:
        print("❌ Gemini did not return a valid repair patch.", file=sys.stderr)
        if pr_number:
            post_pr_comment(pr_number, f"⚠️ **Autonomous Autofix Failed**: Gemini was unable to generate a valid repair patch for the following error:\n```\n{error_snippet[:500]}\n```")
        sys.exit(1)

    summary = repair_result.get("summary", "Applied code repair")
    repaired_files = []

    print(f"==> Applying repair: {summary}")
    for item in repair_result["files"]:
        fpath = item.get("path")
        fcontent = item.get("content")
        if not fpath or fcontent is None:
            continue
        # Security sanity check: prevent path traversal
        norm = os.path.normpath(fpath)
        if norm.startswith("..") or os.path.isabs(norm):
            print(f"Warning: Rejected insecure file path: {fpath}")
            continue
        
        os.makedirs(os.path.dirname(norm), exist_ok=True)
        with open(norm, "w", encoding="utf-8") as out_f:
            out_f.write(fcontent)
        repaired_files.append(norm)
        print(f"  Updated: {norm}")

    # Re-verify locally
    print(f"==> Re-running validation check: '{test_cmd}' in '{test_cwd}'")
    v_code, v_out, v_err = run_cmd(test_cmd, cwd=test_cwd)
    if v_code != 0:
        print("❌ Re-verification failed after applying fix. Rolling back changes.")
        run_cmd("git checkout -- .")
        if pr_number:
            post_pr_comment(pr_number, f"⚠️ **Autofix Verification Failed**: Applied `{summary}`, but `{test_cmd}` still failed. Changes were rolled back.\n```\n{(v_out + v_err)[-300:]}\n```")
        sys.exit(1)

    print("✅ Re-verification passed! Staging and committing changes...")
    run_cmd("git config user.name 'github-actions[bot]'")
    run_cmd("git config user.email '41898282+github-actions[bot]@users.noreply.github.com'")
    run_cmd("git add " + " ".join(repaired_files))
    
    commit_msg = f"fix(autofix): {summary}\n\nAutonomous repair verified by local test runner."
    c_code, c_out, c_err = run_cmd(["git", "commit", "-m", commit_msg])
    if c_code != 0:
        print("No changes to commit or commit failed.")
        sys.exit(0)

    # Get HEAD commit SHA
    _, sha, _ = run_cmd("git rev-parse --short HEAD")
    sha = sha.strip()

    # Push to current branch
    head_ref = os.environ.get("HEAD_REF")
    if head_ref:
        print(f"==> Pushing commit {sha} to origin {head_ref}...")
        p_code, p_out, p_err = run_cmd(f"git push origin HEAD:{head_ref}")
        if p_code != 0:
            print(f"Push failed: {p_err}", file=sys.stderr)
            sys.exit(1)
        print("✅ Pushed successfully!")

    if pr_number:
        comment = (
            f"🤖 **Autonomous AI Repair Applied**\n\n"
            f"**Summary:** {summary}\n\n"
            f"**Files Repaired:**\n" + "\n".join([f"- `{f}`" for f in repaired_files]) + "\n\n"
            f"**Verification:** `{test_cmd}` passed ✅\n"
            f"**Commit:** `{sha}`"
        )
        post_pr_comment(pr_number, comment)

if __name__ == "__main__":
    main()
