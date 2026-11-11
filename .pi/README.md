# Pi configuration

This directory contains the project-level [Pi](https://pi.dev) configuration. After cloning, start Pi from the repository root and approve the project when prompted. Project settings supplement or override the user configuration in `~/.pi/agent/`.

## Standard locations

| Path | Purpose |
|---|---|
| `.pi/settings.json` | Project settings and package declarations |
| `.pi/mcp.json` | Project MCP servers |
| `.pi/extensions/` | Project extensions |
| `.agents/skills/` | Shared project skills, grouped by agent category |
| `.pi/prompts/` | Project prompt templates |
| `.pi/themes/` | Project themes |
| `~/.pi/agent/` | Global Pi configuration, credentials, and models |

Project skills live in `.agents/skills/<category>/<skill-name>/SKILL.md`, not under
`.pi/`. Pi discovers them directly; run `python3 scripts/sync_claude_skills.py --prune`
from the repository root to expose them to Claude Code. See the
[shared skills guide](../.agents/skills/README.md) for naming, preloading, and dry-run details.

## RENCI Azure models

Pi does not load `models.json` from a project `.pi/` directory. Copy or merge this repository's `.pi/models.json` into the global location:

```bash
cp .pi/models.json ~/.pi/agent/models.json
export AZURE_OPENAI_API_KEY="<the RENCI analytics key>"
```

If `~/.pi/agent/models.json` already contains custom providers, merge the `renci-azure` provider instead of overwriting the file.

The variable names are Pi's own for Azure (`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_BASE_URL`), so
the [Pi provider docs](https://pi.dev/docs/latest/providers) apply to where the key goes. Two
things differ from Pi's built-in `azure` provider: the models are RENCI's deployments, listed in
this repository's `renci-azure` provider block, and that block carries the endpoint URL itself
(a custom provider needs a literal `baseUrl`). The pipeline's own scripts read
`AZURE_OPENAI_BASE_URL` with the same RENCI endpoint as the default. If you already use
`AZURE_OPENAI_API_KEY` for another Azure account, set it per project rather than globally.

Every key the pipeline can use, and which step needs it, is listed in [`.env.example`](../.env.example)
at the repository root. Copy it to `.env` (gitignored), fill in what you have, and run with
`uv run --env-file .env ...`; a keychain lookup in the shell works just as well, since the
scripts only ever read environment variables. On the AWS server the same names come from SSM
Parameter Store, never from a file. Never commit a key.

## Subagents

`.pi/settings.json` declares the pinned `@tintinweb/pi-subagents` package. Pi installs it after project trust is granted. To reconcile packages manually, run:

```bash
pi update --extensions --approve
```

The example custom agent is `.pi/agents/example-reviewer.md`. View and run available agents from `/agents` in an interactive Pi session.
