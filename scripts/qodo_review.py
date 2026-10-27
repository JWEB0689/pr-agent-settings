#!/usr/bin/env python3
"""
Qodo-parity PR Summary, Code Review, and Fixer for Jeremy's repositories.

Generates and updates three persistent comments on a pull request:
  1. PR Summary (headline, type/effort badges, AI Description, Mermaid Diagram,
     High-Level Assessment with +/- alternatives & recommendation, and
     semantic Files Changed breakdown with diff links)
  2. Code Review (Bugs / Rule violations / Requirement gaps counters,
     severity-grouped findings with Description, Code & Fix Diff, Evidence,
     copyable AI Agent Prompt, inline commitable suggestions, Sign-Off button,
     Tip of the Day, and the Gemini Muse / Copilot Cat mascot)
  3. Muse Fixer (shows unified diff of all proposed fixes + 1-click Sign-Off
     checkbox & button to commit fixes to the PR branch)
"""

import base64
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-2.5-flash",
]

ASSET_BASE = "https://raw.githubusercontent.com/JWEB0689/pr-agent-settings/main/assets"
MUSE_CAT_URL = f"{ASSET_BASE}/muse-cat.svg"
MUSE_LOGO_URL = f"{ASSET_BASE}/muse-logo.svg"
DIVIDER_URL = "https://www.qodo.ai/wp-content/uploads/2025/11/light-grey-line.svg"

SUMMARY_MARKER = "<!-- muse-qodo:summary -->"
REVIEW_MARKER = "<!-- muse-qodo:review -->"
FIXER_MARKER = "<!-- muse-qodo:fixer -->"

TIPS = [
    "💡 Did you know, you can tick the 'Sign-Off & Commit All Fixes' checkbox in the review or comment /sign-off to apply and commit all fixes in one shot",
    "💡 Did you know, every finding includes a copyable 'Agent prompt' block tailored for AI coding assistants",
    "💡 Did you know, you can comment /review or /describe to re-run Gemini Muse, /ask <question> to ask about the PR, or /sign-off to apply all fixes",
]


def gh_api(path, method="GET", payload=None):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Error: GH_TOKEN/GITHUB_TOKEN not set", file=sys.stderr)
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "gemini-muse-reviewer",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if method == "GET" and "?" not in path:
        url = f"https://api.github.com{path}?per_page=100"
    else:
        url = f"https://api.github.com{path}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")[:300]
        print(f"GitHub API {method} {path} failed ({e.code}): {err}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"GitHub API {method} {path} error: {e}", file=sys.stderr)
        return None


def call_gemini(api_key, model, prompt):
    primary = (model or DEFAULT_MODEL).removeprefix("gemini/")
    candidates = [primary] + [m for m in FALLBACK_MODELS if m != primary]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
        },
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
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
        except Exception as e:
            print(f"Gemini error on {candidate}: {e}", file=sys.stderr)
    return None


def diff_anchor(filename):
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()


def sanitize_mermaid(diagram_text):
    """Ensure Mermaid diagram is valid graph TD without markdown fences."""
    if not diagram_text:
        return 'graph TD\n  PR["Pull Request Changes"] --> Build["Build & Verify"]'
    clean = re.sub(r"^```\w*\s*|\s*```$", "", diagram_text.strip(), flags=re.M).strip()
    if not clean.startswith(("graph ", "flowchart ", "sequenceDiagram")):
        clean = "graph TD\n  " + clean
    return clean


def build_diff_snippet(orig, fixed):
    """Build a unified-style diff block showing - old and + new lines."""
    out = []
    if orig:
        for ln in orig.rstrip("\n").splitlines():
            out.append(f"- {ln}")
    if fixed:
        for ln in fixed.rstrip("\n").splitlines():
            out.append(f"+ {ln}")
    return "\n".join(out) if out else "# No line diff provided"


def render_summary_comment(repo, pr_number, pr_files, analysis):
    s = analysis.get("summary") or {}
    headline = html.escape(s.get("headline") or "Pull request changes summary")
    pr_types = s.get("pr_types") or ["✨ Enhancement"]
    effort = s.get("effort") or "🕐 10-20 Minutes"
    badges = " ".join(f"<code>{html.escape(b)}</code>" for b in pr_types + [effort])

    ai_bullets = s.get("ai_description") or ["Updates repository files as shown in the diff."]
    bullets_text = "\n".join(
        f"• {html.escape(b.lstrip('•-* ').strip())}" for b in ai_bullets
    )

    diagram = sanitize_mermaid(s.get("mermaid_diagram", ""))

    alternatives = s.get("alternatives") or []
    alt_blocks = []
    for idx, alt in enumerate(alternatives, 1):
        atitle = html.escape(alt.get("title") or f"Alternative {idx}")
        pros = [f"- ➕ {html.escape(p)}" for p in (alt.get("pros") or [])]
        cons = [f"- ➖ {html.escape(c)}" for c in (alt.get("cons") or [])]
        items = "\n".join(pros + cons) or "- ➕ Keeps existing implementation."
        alt_blocks.append(
            f"<details open>\n<summary><b>{idx}. {atitle}</b></summary>\n\n"
            f"<dl>\n<dd>\n\n{items}\n\n</dd>\n</dl>\n\n</details>"
        )
    alt_section = "\n\n".join(alt_blocks) if alt_blocks else (
        "<details open>\n<summary><b>1. Keep previous implementation without these changes</b></summary>\n\n"
        "<dl>\n<dd>\n\n- ➕ Zero diff risk.\n- ➖ Leaves the target issue unresolved.\n\n</dd>\n</dl>\n\n</details>"
    )
    recommendation = html.escape(
        s.get("recommendation")
        or "Proceed with this PR's targeted approach as it directly addresses the requirement with minimal blast radius."
    )

    # Group files changed
    file_meta = {f["filename"]: f for f in pr_files}
    total_add = sum(f.get("additions", 0) for f in pr_files)
    total_del = sum(f.get("deletions", 0) for f in pr_files)

    groups = {}
    seen_files = set()
    for item in s.get("files_summary") or []:
        fname = item.get("filename")
        if not fname or fname not in file_meta:
            continue
        seen_files.add(fname)
        grp = item.get("group") or "Other"
        groups.setdefault(grp, []).append((fname, item))

    for f in pr_files:
        fname = f["filename"]
        if fname not in seen_files:
            groups.setdefault("Other", []).append(
                (fname, {"title": f"Update {os.path.basename(fname)}", "bullets": [f"Modifies {fname}."]})
            )

    group_html = []
    for grp_name, entries in groups.items():
        g_add = sum(file_meta[fn].get("additions", 0) for fn, _ in entries)
        g_del = sum(file_meta[fn].get("deletions", 0) for fn, _ in entries)
        file_items_html = []
        for fn, info in entries:
            f_add = file_meta[fn].get("additions", 0)
            f_del = file_meta[fn].get("deletions", 0)
            ftitle = html.escape(info.get("title") or f"Update {os.path.basename(fn)}")
            fbullets = "\n".join(
                f"• {html.escape(b.lstrip('•-* ').strip())}"
                for b in (info.get("bullets") or [f"Updates {fn}."])
            )
            anchor = diff_anchor(fn)
            diff_url = f"https://github.com/{repo}/pull/{pr_number}/files#diff-{anchor}"
            base_fn = html.escape(os.path.basename(fn))
            file_items_html.append(
                f"<details>\n"
                f"<summary>{base_fn}<code>{ftitle}</code> <code>+{f_add}/-{f_del}</code></summary>\n\n"
                f"<br/>\n\n"
                f">{ftitle}\n>\n"
                f"><pre>\n>{fbullets}\n></pre>\n>\n"
                f"><a href='{diff_url}'>{html.escape(fn)}</a>\n\n"
                f"</details>"
            )
        joined_files = "\n\n".join(file_items_html)
        group_html.append(
            f"<details open>\n"
            f"<summary>{html.escape(grp_name)} ({len(entries)}) <code> +{g_add} / -{g_del} </code></summary>\n\n"
            f"<dl>\n<dd>\n\n{joined_files}\n\n</dd>\n</dl>\n\n</details>"
        )

    files_section = "\n\n".join(group_html)

    return f"""{SUMMARY_MARKER}
<h3><img src="{MUSE_CAT_URL}" width="28" align="absmiddle" alt="Gemini Muse"> PR Summary by Gemini Muse</h3>

{headline}

{badges}

<img src="{DIVIDER_URL}" height="10%" alt="Grey Divider">

<details open>
<summary>AI Description</summary>

<dl>
<dd>
<br/>

><pre>
>{bullets_text}
></pre>

</dd>
</dl>

</details>

<details open>
<summary>Diagram</summary>

<dl>
<dd>

<br/>

```mermaid
{diagram}
```

</dd>
</dl>

</details>

<details open>
<summary>High-Level Assessment</summary>

<dl>
<dd>

<br/>

>The following are alternative approaches to this PR:

{alt_section}

>**Recommendation:** {recommendation}

</dd>
</dl>

</details>

<details>
<summary> Files changed ({len(pr_files)}) <code> +{total_add} / -{total_del} </code> </summary>

<dl>
<dd>

<br/>

{files_section}

</dd>
</dl>

</details>
"""


def render_review_comment(repo, pr_number, head_sha, analysis):
    rev = analysis.get("review") or {}
    findings = rev.get("findings") or []

    bugs = sum(1 for f in findings if "bug" in (f.get("category") or "").lower())
    rules = sum(1 for f in findings if "rule" in (f.get("category") or "").lower())
    gaps = sum(1 for f in findings if "gap" in (f.get("category") or "").lower() or "req" in (f.get("category") or "").lower())
    other = len(findings) - (bugs + rules + gaps)
    if other > 0:
        bugs += other

    pills = (
        f"<code>🐞 Bugs ({bugs})</code>  "
        f"<code>📘 Rule violations ({rules})</code>  "
        f"<code>📎 Requirement gaps ({gaps})</code>"
    )

    tip = TIPS[int(pr_number or 0) % len(TIPS)]

    if not findings:
        body_core = f"""<img src="{MUSE_CAT_URL}" width="18%" alt="Gemini Muse Cat">

<h3>Great, no issues found!</h3>
Gemini Muse reviewed your code and found no material issues that require review"""
    else:
        sev_groups = {"High": [], "Medium": [], "Low": []}
        for f in findings:
            s = (f.get("severity") or "Medium").capitalize()
            if s not in sev_groups:
                s = "Medium"
            sev_groups[s].append(f)

        sev_badges = {
            "High": ('<img src="https://img.shields.io/badge/High-CE2C31?style=flat-square" height="20px" alt="Action required">', "🔴 High"),
            "Medium": ('<img src="https://img.shields.io/badge/Medium-E36209?style=flat-square" height="20px" alt="Remediation recommended">', "🟠 Medium"),
            "Low": ('<img src="https://img.shields.io/badge/Low-D4A72C?style=flat-square" height="20px" alt="Informational">', "🟡 Low"),
        }

        sections = []
        idx = 1
        for sev in ("High", "Medium", "Low"):
            badge_img, sev_pill = sev_badges[sev]
            group = sev_groups[sev]
            if not group:
                sections.append(f"{badge_img}\n\n_No findings for this group_\n")
                continue
            items_html = [badge_img]
            for f in group:
                title = html.escape(f.get("title") or "Code issue")
                cat = html.escape(f.get("category") or "🐞 Bug")
                aspect = html.escape(f.get("aspect") or "≡ Correctness")
                fpath = f.get("file") or "unknown"
                st = int(f.get("start_line") or 1)
                en = int(f.get("end_line") or st)
                desc = html.escape(f.get("description") or "")
                evid = html.escape(f.get("evidence") or "Identified during static analysis of the PR diff.")
                orig = f.get("original_code") or ""
                fixed = f.get("fixed_code") or ""
                diff_block = build_diff_snippet(orig, fixed)

                anchor = diff_anchor(fpath)
                diff_link = f"https://github.com/{repo}/pull/{pr_number}/files#diff-{anchor}R{st}-R{en}"
                blob_link = f"https://github.com/{repo}/blob/{head_sha}/{fpath}#L{st}-L{en}"

                issue_sum = f.get("issue_summary") or f.get("description") or title
                rec_fix = f.get("recommended_fix") or "Apply the suggested diff above."

                agent_prompt = (
                    "The issue below was found during a code review. Follow the provided context and guidance below and implement a solution\n\n"
                    f"## Issue description\n{issue_sum}\n\n"
                    f"## Fix Focus Areas\n- {fpath}[{st}-{en}]\n\n"
                    f"## Recommended Fix\n{rec_fix}"
                )

                items_html.append(f"""<details open>
<summary>  {idx}.  {title} <code>{sev_pill}</code> <code>{cat}</code> <code>{aspect}</code></summary>

<br/>

> <details open>
><summary>Description</summary>
><br/>
>
><pre>
>{desc}
></pre>
></details>

> <details open>
><summary>Code &amp; Fix Diff</summary>
><br/>
>
><code>[{html.escape(fpath)}[R{st}-{en}]]({diff_link})</code>
>
>```diff
>{diff_block}
>```
></details>

> <details>
><summary>Evidence</summary>
><br/>
>
><pre>
>{evid}
></pre>
>
> <code>[{html.escape(fpath)}\\[{st}-{en}\\]]({blob_link})</code>
></details>

> <details open>
><summary>Agent prompt</summary>
><br/>
>
>```
>{agent_prompt}
>```
> <code>ⓘ Copy this prompt and use it to remediate the issue with your preferred AI generation tools</code>
></details>

<hr/>
</details>""")
                idx += 1
            sections.append("\n\n".join(items_html))

        signoff_box = (
            f"### ✍️ Sign-Off & Commit Fixes\n\n"
            f"- [ ] <!-- muse-sign-off --> **Sign-Off & Commit All Fixes** — "
            f"Tick this checkbox (or comment `/sign-off`) to automatically apply, verify, and commit the {len(findings)} fix(es) above to this PR branch.\n\n"
            f"<a href=\"https://github.com/{repo}/pull/{pr_number}#issuecomment-new\">"
            f"<img src=\"https://img.shields.io/badge/✍️_Sign--Off_%26_Commit_Fixes-2ea44f?style=for-the-badge&logo=github\" alt=\"Sign-Off & Commit Fixes\"></a> "
            f"<a href=\"https://github.com/{repo}/actions\">"
            f"<img src=\"https://img.shields.io/badge/⚡_Run_Auto--Fixer-4285F4?style=for-the-badge&logo=google-gemini&logoColor=white\" alt=\"Run Auto-Fixer\"></a>"
        )
        body_core = "\n\n<br/>\n\n".join(sections) + "\n\n<br/>\n\n" + signoff_box

    return f"""{REVIEW_MARKER}
<h3><img src="{MUSE_CAT_URL}" width="28" align="absmiddle" alt="Gemini Muse"> Code Review by Gemini Muse</h3>

{pills}

<img src="{DIVIDER_URL}" height="10%" alt="Grey Divider">

<br/>

{body_core}

<img src="{DIVIDER_URL}" height="10%" alt="Grey Divider">

<!-- qodo-daily-tip:start -->
<details>
<summary><strong>Tip of the day</strong></summary>

<br/>

<pre>{tip}</pre>

</details>

<img src="{DIVIDER_URL}" height="10%" alt="Grey Divider">
<!-- qodo-daily-tip:end -->

<!-- https://github.com/{repo}/commit/{head_sha} -->

<a href="https://github.com/JWEB0689/pr-agent-settings"><img src="{MUSE_LOGO_URL}" width="170" alt="Gemini Muse Logo"></a>
"""


def finding_id(f):
    raw = f"{f.get('file')}:{f.get('start_line') or 0}-{f.get('end_line') or 0}:{f.get('title') or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def render_fixer_comment(repo, pr_number, findings):
    if not findings:
        return f"""{FIXER_MARKER}
### <img src="{MUSE_CAT_URL}" width="24" align="absmiddle" alt="Muse"> Gemini Muse Fixer

No findings are available for this PR yet. Findings appear here once Gemini Muse identifies actionable fixes.
"""

    fix_payload = []
    sections = []
    for f in findings:
        fpath = f.get("file")
        st = int(f.get("start_line") or 0)
        en = int(f.get("end_line") or st)
        orig = f.get("original_code") or ""
        fixed = f.get("fixed_code") or ""
        if fpath and fixed:
            fid = finding_id(f)
            title = f.get("title") or "Apply review fix"
            diff = build_diff_snippet(orig, fixed)
            fix_payload.append({
                "path": fpath,
                "start": st,
                "end": en,
                "original": orig,
                "code": fixed,
                "title": title,
                "id": fid,
            })
            sections.append(
                f"<details open>\n"
                f"<summary><b>Fix {len(sections) + 1}: {html.escape(title)}</b> — <code>{html.escape(fpath)} lines {st}-{en}</code></summary>\n\n"
                f"```diff\n{diff}\n```\n\n"
                f"</details>\n\n"
                f"- [ ] <!-- muse-fix:{fid} --> ✅ **Apply this fix** — commit just this change to the PR branch\n"
                f"- [ ] <!-- muse-resolve:{fid} --> 🗑️ **Resolve / dismiss** — close this finding without applying it\n"
            )

    encoded_fixes = base64.b64encode(json.dumps(fix_payload).encode("utf-8")).decode("ascii")
    per_fix = "\n".join(sections) if sections else "# See review findings above"

    return f"""{FIXER_MARKER}
<!-- muse-fixes-b64:{encoded_fixes} -->
### <img src="{MUSE_CAT_URL}" width="24" align="absmiddle" alt="Muse"> Gemini Muse Fixer

✅ Committed (0) · 🔧 Ready to Fix ({len(fix_payload)})

Each fix below shows its exact diff. Tick **Apply this fix** to commit just that one, or **Resolve / dismiss** to close the finding without applying it — or use Sign-Off to commit them all at once.

{per_fix}
<br/>

- [ ] <!-- muse-sign-off --> 🚀 **Sign-Off & Commit All Fixes** — Check this box (or comment `/sign-off`) to apply all the fixes above, run verification, and commit directly to this PR branch.

<a href="https://github.com/{repo}/pull/{pr_number}#issuecomment-new"><img src="https://img.shields.io/badge/✍️_Sign--Off_%26_Commit_Fixes-2ea44f?style=for-the-badge&logo=github" alt="Sign-Off & Commit Fixes"></a>
"""

def upsert_comment(repo, pr_number, marker, body, existing_comments):
    for c in existing_comments:
        cbody = c.get("body") or ""
        if marker in cbody:
            cid = c["id"]
            gh_api(f"/repos/{repo}/issues/comments/{cid}", method="PATCH", payload={"body": body})
            print(f"Updated existing comment {cid} ({marker})")
            return cid
    res = gh_api(f"/repos/{repo}/issues/{pr_number}/comments", method="POST", payload={"body": body})
    cid = (res or {}).get("id")
    print(f"Created new comment {cid} ({marker})")
    return cid


def cleanup_legacy_pr_agent_comments(repo, existing_comments):
    """Remove plain old-style PR-Agent comments so only Qodo-style comments remain."""
    legacy_markers = (
        "<!-- pr-agent:review:full -->",
        "<!-- pr-agent:improve:no-suggestions -->",
        "<!-- pr-agent:improve:summary -->",
        "Failed to generate code suggestions for PR",
    )
    for c in existing_comments:
        cbody = c.get("body") or ""
        author = (c.get("user") or {}).get("login") or ""
        if "github-actions" in author and (
            any(m in cbody for m in legacy_markers)
            or cbody.startswith("**[Persistent review](")
            or cbody.startswith("**[PR Description](")
        ):
            cid = c["id"]
            gh_api(f"/repos/{repo}/issues/comments/{cid}", method="DELETE")
            print(f"Removed legacy PR-Agent comment {cid}")


def post_inline_suggestions(repo, pr_number, head_sha, findings, pr_files):
    """Post inline GitHub ```suggestion comments for findings on changed lines."""
    if not findings:
        return
    valid_files = {f["filename"] for f in pr_files}
    comments = []
    for f in findings:
        fpath = f.get("file")
        fixed = f.get("fixed_code")
        if not fpath or fpath not in valid_files or fixed is None:
            continue
        try:
            st = int(f.get("start_line") or 0)
            en = int(f.get("end_line") or st)
        except (TypeError, ValueError):
            continue
        if st <= 0 or en < st:
            continue
        title = f.get("title") or "Suggested Fix"
        desc = f.get("description") or ""
        c_item = {
            "path": fpath,
            "line": en,
            "side": "RIGHT",
            "body": f"**{title}** ({f.get('severity', 'Medium')})\n\n{desc}\n\n```suggestion\n{fixed.rstrip()}\n```",
        }
        if st < en:
            c_item["start_line"] = st
            c_item["start_side"] = "RIGHT"
        comments.append(c_item)

    if not comments:
        return
    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "comments": comments[:10],
    }
    gh_api(f"/repos/{repo}/pulls/{pr_number}/reviews", method="POST", payload=payload)


REVIEW_PROMPT_SCHEMA = """You are Qodo / Gemini Muse, an elite AI code reviewer. Analyze the pull request below and return ONLY a valid JSON object matching this exact schema:
{
  "summary": {
    "headline": "Concise 1-line imperative summary of the PR (e.g. 'Restore ARM64 APK builds with Tailwind v3 compatibility')",
    "pr_types": ["🐞 Bug fix", "⚙️ Configuration changes"],
    "effort": "🕐 10-20 Minutes",
    "ai_description": [
      "First key accomplishment bullet",
      "Second key accomplishment bullet"
    ],
    "mermaid_diagram": "graph TD\\n  A[\\"Step 1\\"] --> B[\\"Step 2\\"]",
    "alternatives": [
      {
        "title": "Alternative approach title",
        "pros": ["Advantage of the alternative"],
        "cons": ["Tradeoff or downside of the alternative"]
      }
    ],
    "recommendation": "Clear recommendation explaining why this PR's approach or an alternative should be preferred.",
    "files_summary": [
      {
        "filename": "exact/path/from/diff",
        "group": "Bug fix | Enhancement | Configuration | Other",
        "title": "Short imperative summary of changes in this file",
        "bullets": ["Detailed sentence describing what changed in this file and why."]
      }
    ]
  },
  "review": {
    "findings": [
      {
        "title": "Short 4-7 word finding title",
        "severity": "High | Medium | Low",
        "category": "🐞 Bug | 📘 Rule violation | 📎 Requirement gap",
        "aspect": "≡ Correctness | ⛨ Security | ☼ Reliability | ⚡ Performance",
        "file": "exact/path/from/diff",
        "start_line": 10,
        "end_line": 12,
        "description": "Clear explanation of the bug, security flaw, or requirement gap.",
        "evidence": "Concrete evidence from the diff and surrounding file context.",
        "original_code": "exact original lines 10-12 from the new file",
        "fixed_code": "exact replacement lines that fix the issue",
        "issue_summary": "1-2 sentence summary of the problem for an AI coding agent.",
        "recommended_fix": "Specific instructions on how to remediate the issue."
      }
    ]
  }
}

Rules:
1. If the PR is clean and has no material bugs, security issues, or broken logic, return `"findings": []`. Do NOT invent nitpicks.
2. In `mermaid_diagram`, ALWAYS quote node labels like `NodeId["Label text"]` so parentheses or slashes never break Mermaid parsing. Use `graph TD`.
3. For every finding, `start_line` and `end_line` must be 1-based line numbers in the NEW version of `file`, `original_code` must match those lines, and `fixed_code` must be the exact drop-in fix.
"""


def run_review(repo, pr_number, api_key, model=DEFAULT_MODEL):
    pr = gh_api(f"/repos/{repo}/pulls/{pr_number}")
    if not pr:
        print(f"Could not fetch PR #{pr_number} in {repo}", file=sys.stderr)
        return False

    head_sha = ((pr.get("head") or {}).get("sha")) or ""
    pr_title = pr.get("title") or ""
    pr_body = (pr.get("body") or "")[:3000]
    pr_files = gh_api(f"/repos/{repo}/pulls/{pr_number}/files") or []

    diff_parts = []
    for f in pr_files[:20]:
        fn = f.get("filename")
        patch = (f.get("patch") or "")[:4000]
        diff_parts.append(
            f"=== FILE: {fn} (+{f.get('additions', 0)}/-{f.get('deletions', 0)}) ===\n{patch}"
        )
    diff_context = "\n\n".join(diff_parts)[:30000]

    file_contexts = {}
    for f in pr_files[:8]:
        fn = f.get("filename")
        if fn and os.path.isfile(fn) and os.path.getsize(fn) < 60000:
            try:
                with open(fn, "r", encoding="utf-8", errors="ignore") as fh:
                    numbered = "\n".join(
                        f"{i}: {line}" for i, line in enumerate(fh.read().splitlines(), 1)
                    )
                    file_contexts[fn] = numbered[:12000]
            except Exception:
                pass

    prompt = (
        f"{REVIEW_PROMPT_SCHEMA}\n\n"
        f"PR TITLE: {pr_title}\n"
        f"PR DESCRIPTION:\n{pr_body}\n\n"
        f"--- CHANGED FILES DIFF ---\n{diff_context}\n--- END DIFF ---\n"
    )
    if file_contexts:
        prompt += f"\n--- NUMBERED FILE CONTENTS (NEW VERSION) ---\n{json.dumps(file_contexts, indent=2)}\n"

    print(f"==> Querying Gemini ({model}) for Qodo-style PR Summary & Code Review...")
    analysis = call_gemini(api_key, model, prompt)
    if not analysis or not isinstance(analysis, dict):
        print("Gemini failed to return valid analysis JSON.", file=sys.stderr)
        return False

    existing_comments = gh_api(f"/repos/{repo}/issues/{pr_number}/comments") or []
    cleanup_legacy_pr_agent_comments(repo, existing_comments)

    summary_md = render_summary_comment(repo, pr_number, pr_files, analysis)
    review_md = render_review_comment(repo, pr_number, head_sha, analysis)
    findings = (analysis.get("review") or {}).get("findings") or []
    fixer_md = render_fixer_comment(repo, pr_number, findings)

    upsert_comment(repo, pr_number, SUMMARY_MARKER, summary_md, existing_comments)
    upsert_comment(repo, pr_number, REVIEW_MARKER, review_md, existing_comments)
    upsert_comment(repo, pr_number, FIXER_MARKER, fixer_md, existing_comments)

    if findings:
        post_inline_suggestions(repo, pr_number, head_sha, findings, pr_files)

    return True


def _self_test():
    sample_files = [
        {"filename": "package.json", "additions": 1, "deletions": 1},
        {"filename": "package-lock.json", "additions": 561, "deletions": 46},
    ]
    sample_analysis = {
        "summary": {
            "headline": "Restore ARM64 APK builds with Tailwind v3 compatibility",
            "pr_types": ["🐞 Bug fix", "⚙️ Configuration changes"],
            "effort": "🕐 10-20 Minutes",
            "ai_description": [
                "Restore compatibility between Tailwind and the existing PostCSS configuration to unblock the ARM64 APK build.",
                "Update the lockfile so CI installs a compatible Tailwind v3 dependency graph.",
            ],
            "mermaid_diagram": 'graph TD\n  A["package.json"] --> B["Tailwind v3"] --> C["ARM64 APK Build"]',
            "alternatives": [
                {
                    "title": "Migrate the styling pipeline to Tailwind v4",
                    "pros": ["Retains the newer Tailwind major version."],
                    "cons": ["Requires coordinated PostCSS, CSS, and theme changes."],
                }
            ],
            "recommendation": "Use the PR's Tailwind v3 alignment for this targeted build fix.",
            "files_summary": [
                {
                    "filename": "package.json",
                    "group": "Bug fix",
                    "title": "Set Tailwind's supported major version to v3",
                    "bullets": ["Changes the Tailwind devDependency range from ^4.3.3 to ^3.4.17."],
                }
            ],
        },
        "review": {
            "findings": [
                {
                    "title": "Unquoted version variable in shell step",
                    "severity": "High",
                    "category": "🐞 Bug",
                    "aspect": "⛨ Security",
                    "file": "package.json",
                    "start_line": 12,
                    "end_line": 12,
                    "description": "Direct interpolation allows shell expansion.",
                    "evidence": "Line 12 interpolates input directly.",
                    "original_code": '  "tailwindcss": "^4.3.3"',
                    "fixed_code": '  "tailwindcss": "^3.4.17"',
                    "issue_summary": "Tailwind v4 breaks PostCSS build.",
                    "recommended_fix": "Pin tailwindcss to ^3.4.17.",
                }
            ]
        },
    }
    s_md = render_summary_comment("JWEB0689/KernelForge-App", 67, sample_files, sample_analysis)
    r_md = render_review_comment("JWEB0689/KernelForge-App", 67, "deadbeef", sample_analysis)
    f_md = render_fixer_comment("JWEB0689/KernelForge-App", 67, sample_analysis["review"]["findings"])
    assert "PR Summary by Gemini Muse" in s_md
    assert "```mermaid" in s_md
    assert "High-Level Assessment" in s_md
    assert "Code Review by Gemini Muse" in r_md
    assert "Agent prompt" in r_md
    assert "```diff" in r_md
    assert "muse-sign-off" in r_md
    assert "muse-fixes-b64:" in f_md
    print("Self-test passed!")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
        sys.exit(0)
    api_key = os.environ.get("GEMINI_API_KEY")
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY")
    pr_number = os.environ.get("PR_NUMBER")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    if not (api_key and repo and pr_number):
        print("Usage: requires GEMINI_API_KEY, REPO, and PR_NUMBER env vars.", file=sys.stderr)
        sys.exit(1)
    ok = run_review(repo, pr_number, api_key, model)
    sys.exit(0 if ok else 1)
