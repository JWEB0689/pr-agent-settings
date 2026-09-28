# PR-Agent Central Configuration

This repository houses the central configuration file (`.pr_agent.toml`) and reusable workflow for [PR-Agent](https://github.com/the-pr-agent/pr-agent) across all repositories owned by [@JWEB0689](https://github.com/JWEB0689).

## How It Works

1. **Global Settings Inheritance**:
   PR-Agent automatically looks up `JWEB0689/pr-agent-settings` and loads `.pr_agent.toml` on every PR across all repositories owned by `@JWEB0689`.
   See the [PR-Agent Global Configuration Documentation](https://docs.pr-agent.ai/usage-guide/configuration_options/#global-configuration-file).

2. **Repository Overrides**:
   Any repository can define its own `.pr_agent.toml` to customize tool behavior. Repository-level configurations override these global defaults.

3. **Reusable Workflow**:
   A reusable workflow is available at `.github/workflows/pr_agent.yml` which any repository can call, or repositories can use their standalone workflow.

4. **Interactive Commands**:
   You can trigger PR-Agent on any Pull Request by commenting:
   - `/review` — Perform full code review with security and test analysis.
   - `/describe` — Generate comprehensive PR description and changelog.
   - `/improve` — Provide commit-ready inline code suggestions.
   - `/ask <question>` — Ask specific questions about the PR changes.
