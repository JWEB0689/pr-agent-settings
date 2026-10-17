# PR-Agent Central Configuration (`Gemini Muse`)

<p align="center">
  <img src="assets/muse-cat.svg" width="96" alt="Gemini Muse Cat" />
  <br/>
  <img src="assets/muse-logo.svg" width="220" alt="Gemini Muse Logo" />
</p>

This repository houses the central configuration (`.pr_agent.toml`), Qodo-parity review engine ([`scripts/qodo_review.py`](scripts/qodo_review.py)), autonomous repair engine ([`scripts/autofix.py`](scripts/autofix.py)), and reusable GitHub Actions workflows across all repositories owned by [@JWEB0689](https://github.com/JWEB0689).

## What Gets Posted on Every PR

On every Pull Request (`opened`, `reopened`, `synchronize`, or `/review`), **Gemini Muse** posts and updates three Qodo-style comments:

1. **PR Summary by Gemini Muse**:
   - One-line headline + type & effort badges (`🐞 Bug fix`, `⚙️ Configuration changes`, `🕐 10-20 Minutes`)
   - **AI Description** bullet summary
   - **Diagram** (`mermaid` `graph TD` flowchart of the PR changes)
   - **High-Level Assessment** with alternative approaches (`➕` pros / `➖` cons) and a **Recommendation**
   - **Files changed (`+X / -Y`)** grouped semantically (`Bug fix`, `Enhancement`, `Configuration`, `Other`) with per-file summaries and direct `#diff-...` links

2. **Code Review by Gemini Muse**:
   - Category counters: `🐞 Bugs (X)` · `📘 Rule violations (Y)` · `📎 Requirement gaps (Z)`
   - **Zero-issues state**: Displays the **Gemini Muse Cat** illustration + *"Great, no issues found!"*
   - **Actionable findings**: Grouped by severity (`High`, `Medium`, `Low`), each expandable with:
     - **Description** of the bug/risk
     - **Code & Fix Diff** (`diff` block showing `-` original and `+` fixed code + file line links)
     - **Evidence** from the diff/codebase
     - **Agent prompt** — ready-to-copy prompt block (`## Issue description`, `## Fix Focus Areas`, `## Recommended Fix`) for AI coding tools
   - **Inline GitHub `suggestion` comments** for one-click per-line commits

3. **Gemini Muse Fixer & 1-Click Sign-Off**:
   - Displays the combined **Proposed Fix Diff** across all flagged files
   - **1-Click Sign-Off Checkbox**: Tick `- [ ] 🚀 Sign-Off & Commit All Fixes` directly in the comment (or comment `/sign-off` / `/autofix`) to apply all fixes, verify with the repo's test runner, and commit to the PR branch.

## Per-Repository Setup

Copy [`.github/workflows/pr-agent-repo-trigger.yml`](.github/workflows/pr-agent-repo-trigger.yml) into the target repo's `.github/workflows/` directory and ensure the `GEMINI_API_KEY` secret is configured.
