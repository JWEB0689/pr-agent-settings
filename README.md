# PR-Agent Central Configuration

This repository houses the central configuration file (`.pr_agent.toml`) and reusable workflows for [PR-Agent](https://github.com/the-pr-agent/pr-agent) across all repositories owned by [@JWEB0689](https://github.com/JWEB0689).

## How It Works

1. **Global Settings Inheritance**:
   PR-Agent automatically looks up `JWEB0689/pr-agent-settings` and loads `.pr_agent.toml` on every PR across all repositories owned by `@JWEB0689`.
   See the [PR-Agent Global Configuration Documentation](https://docs.pr-agent.ai/usage-guide/configuration_options/#global-configuration-file).

2. **Repository Overrides**:
   Any repository can define its own `.pr_agent.toml` to customize tool behavior. Repository-level configurations override these global defaults.

3. **Reusable Workflows**:
   - `.github/workflows/pr_agent.yml` — PR-Agent auto-review (called by per-repo workflows).
   - `.github/workflows/reusable_autofix.yml` — autonomous CI auto-fixer with `pr` and `scheduled` modes.

4. **Interactive Commands**:
   Comment on any Pull Request:
   - `/review` — Full code review with security, test, risk, effort, and merge-recommendation analysis.
   - `/describe` — PR description, changelog, and Mermaid diagram walkthrough.
   - `/improve` — Commit-ready inline code suggestions.
   - `/ask <question>` — Ask specific questions about the PR changes.
   - `/sign-off` — **Apply every PR-Agent code suggestion and commit in one shot** (see setup below).

## Per-Repository Setup (required for PR-Agent to run)

Each repository must add a trigger workflow that calls the reusable workflows here. Copy `.github/workflows/pr-agent-repo-trigger.yml` from this repo into the target repo's `.github/workflows/` directory:

```yaml
name: PR-Agent + Sign-off

on:
  pull_request:
    types: [opened, reopened, synchronize]
  issue_comment:
    types: [created]

permissions:
  contents: write
  pull-requests: write
  issues: write

jobs:
  pr_agent:
    if: ${{ github.event_name == 'pull_request' || (github.event.issue.pull_request && github.event.sender.type != 'Bot') }}
    uses: JWEB0689/pr-agent-settings/.github/workflows/pr_agent.yml@main
    secrets:
      GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}

  sign-off:
    if: ${{ github.event_name == 'issue_comment' && github.event.issue.pull_request && contains(github.event.comment.body, '/sign-off') && github.event.sender.type != 'Bot' }}
    uses: JWEB0689/pr-agent-settings/.github/workflows/reusable_autofix.yml@main
    with:
      mode: pr
      pr_number: ${{ github.event.issue.number }}
    secrets:
      GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
```

Then add the `GEMINI_API_KEY` secret to each repo: Settings → Secrets and variables → Actions → New repository secret.

## Sign-Off Flow

1. PR-Agent posts `/review` findings with GitHub **Commit suggestion** buttons on each fix.
2. Comment `/sign-off` on the PR to apply **all** suggestions at once and commit them to the PR branch.
3. The autofix engine verifies the changes with the repo's test/build command before committing; on failure it rolls back and comments on the PR.

## Diagram Walkthrough

`/describe` generates a PR description with a Mermaid diagram walkthrough (`enable_pr_diagram = true` in `.pr_agent.toml`).

## Model

Uses Google AI Studio Gemini via `GEMINI_API_KEY`. Current model: `gemini/gemini-2.5-flash` with fallbacks `gemini/gemini-2.0-flash`, `gemini/gemini-1.5-flash`, `gemini/gemini-2.5-flash-lite`.
