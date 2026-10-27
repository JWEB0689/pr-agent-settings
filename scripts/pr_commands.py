#!/usr/bin/env python3
"""Gemini Muse PR commands: /ask and /describe.

Invoked by the reusable pr_agent.yml workflow on issue_comment events whose
body starts with /ask or /describe.

  /ask <question>  — answer a question about the PR, grounded in its diff.
  /describe        — (re)generate the PR description: the problem it solves,
                     what the change does, and the expected result. Written
                     into a marked section of the PR body (idempotent).

Env: GH_TOKEN/GITHUB_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, REPO, PR_NUMBER,
COMMENT_BODY.
"""
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = ["gemini-2.5-flash"]
DIFF_CAP = 60000
DESC_START = "<!-- gemini-muse:describe:start -->"
DESC_END = "<!-- gemini-muse:describe:end -->"


def gh_api(path, method="GET", payload=None, accept="application/vnd.github+json"):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Error: GH_TOKEN/GITHUB_TOKEN not set", file=sys.stderr)
        return None
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "gemini-muse-commands",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            if accept == "application/vnd.github+json":
                return json.loads(raw) if raw else {}
            return raw
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")[:300]
        print(f"GitHub API {method} {path} failed ({e.code}): {err}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"GitHub API {method} {path} error: {e}", file=sys.stderr)
        return None


def call_gemini_text(api_key, model, prompt):
    primary = (model or DEFAULT_MODEL).removeprefix("gemini/")
    candidates = [primary] + [m for m in FALLBACK_MODELS if m != primary]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    for candidate in candidates:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{candidate}:generateContent?key={api_key}"
        )
        req = urllib.request.Request(
            url, data=body_bytes, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            print(f"Gemini error on {candidate}: {e}", file=sys.stderr)
    return None


def fetch_context(repo, pr_number):
    pr = gh_api(f"/repos/{repo}/pulls/{pr_number}")
    if not pr:
        return None
    diff = gh_api(
        f"/repos/{repo}/pulls/{pr_number}", accept="application/vnd.github.v3.diff"
    ) or ""
    if len(diff) > DIFF_CAP:
        diff = diff[:DIFF_CAP] + "\n\n[diff truncated for length]"
    commits = gh_api(f"/repos/{repo}/pulls/{pr_number}/commits?per_page=30") or []
    commit_lines = []
    for c in commits:
        msg = (c.get("commit") or {}).get("message", "").splitlines()
        if msg:
            commit_lines.append(f"- {msg[0]}")
    files = gh_api(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100") or []
    file_lines = [
        f"- {f.get('filename')} ({f.get('status')}, +{f.get('additions', 0)}/-{f.get('deletions', 0)})"
        for f in files
    ]
    meta = gh_api(f"/repos/{repo}") or {}
    return {
        "pr": pr,
        "diff": diff,
        "commits": "\n".join(commit_lines) or "(none)",
        "files": "\n".join(file_lines) or "(none)",
        "repo_about": meta.get("description") or "(no repo description set)",
        "repo_lang": meta.get("language") or "unknown",
    }


def context_block(repo, ctx):
    pr = ctx["pr"]
    return (
        f"Repository: {repo} (primary language: {ctx['repo_lang']})\n"
        f"What this repo does: {ctx['repo_about']}\n\n"
        f"PR title: {pr.get('title', '')}\n"
        f"Current PR body:\n{(pr.get('body') or '(empty)')[:4000]}\n\n"
        f"Commits:\n{ctx['commits']}\n\n"
        f"Changed files:\n{ctx['files']}\n\n"
        f"Diff:\n```diff\n{ctx['diff']}\n```"
    )


def do_ask(repo, pr_number, question, api_key, model):
    ctx = fetch_context(repo, pr_number)
    if not ctx:
        return False
    prompt = (
        "You are Gemini Muse, a code reviewer answering a question about a "
        "GitHub pull request. Answer directly and concisely in Markdown, "
        "grounded in the diff below. Reference specific files when relevant. "
        "If the diff does not contain enough information to answer, say so "
        "plainly instead of guessing.\n\n"
        f"{context_block(repo, ctx)}\n\n"
        f"Question: {question}"
    )
    answer = call_gemini_text(api_key, model, prompt)
    if not answer:
        return False
    body = f"💬 **Gemini Muse — /ask**\n\n> {question}\n\n{answer}"
    return gh_api(
        f"/repos/{repo}/issues/{pr_number}/comments",
        method="POST",
        payload={"body": body},
    ) is not None


def do_describe(repo, pr_number, api_key, model):
    ctx = fetch_context(repo, pr_number)
    if not ctx:
        return False
    prompt = (
        "You are Gemini Muse. Write a clear description of this pull "
        "request in Markdown with exactly these three sections:\n\n"
        "### 🎯 Problem\n"
        "The problem or need this PR addresses (infer it from the change, "
        "commits, and what the repo does).\n\n"
        "### 🔧 What this PR does\n"
        "What the change actually does, concretely, file by file where it "
        "matters.\n\n"
        "### ✅ Expected result\n"
        "What should be true once this is merged — the behavior the repo "
        "should have.\n\n"
        "Be specific and factual; do not invent details that are not in the "
        "diff. Output only the three sections, no preamble.\n\n"
        f"{context_block(repo, ctx)}"
    )
    text = call_gemini_text(api_key, model, prompt)
    if not text:
        return False
    section = (
        f"{DESC_START}\n## 📝 Description — Gemini Muse\n\n{text}\n{DESC_END}"
    )
    old_body = ctx["pr"].get("body") or ""
    if DESC_START in old_body and DESC_END in old_body:
        pre = old_body.split(DESC_START)[0].rstrip()
        post = old_body.split(DESC_END, 1)[1].lstrip("\n")
        new_body = (pre + "\n\n" if pre else "") + section + (
            "\n\n" + post if post.strip() else ""
        )
    elif old_body.strip():
        new_body = section + "\n\n---\n\n" + old_body
    else:
        new_body = section
    ok = gh_api(
        f"/repos/{repo}/pulls/{pr_number}",
        method="PATCH",
        payload={"body": new_body},
    )
    if ok is None:
        return False
    gh_api(
        f"/repos/{repo}/issues/{pr_number}/comments",
        method="POST",
        payload={"body": "📝 PR description updated by Gemini Muse (`/describe`)."},
    )
    return True


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY")
    pr_number = os.environ.get("PR_NUMBER")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    comment = (os.environ.get("COMMENT_BODY") or "").strip()
    if not (api_key and repo and pr_number):
        print("Error: GEMINI_API_KEY, REPO, PR_NUMBER are required", file=sys.stderr)
        return 1
    pr_number = int(pr_number)
    low = comment.lower()
    if low.startswith("/ask"):
        question = comment[4:].strip() or (
            "Explain what this PR does and flag anything risky."
        )
        print(f"/ask on {repo}#{pr_number}: {question[:80]}")
        return 0 if do_ask(repo, pr_number, question, api_key, model) else 1
    if low.startswith("/describe"):
        print(f"/describe on {repo}#{pr_number}")
        return 0 if do_describe(repo, pr_number, api_key, model) else 1
    print("No command found in comment; nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
