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

## Models

Pi works with any provider it supports. If you have an Anthropic or OpenAI key, Pi's built-in
providers need no setup here: see the [Pi provider docs](https://pi.dev/docs/latest/providers).

`.pi/models.json` adds one custom provider, `renci-azure`: the Azure OpenAI deployments the
project team uses, with their prices. Pi does not load `models.json` from a project `.pi/`
directory, so to use it, copy or merge it into the global location:

```bash
cp .pi/models.json ~/.pi/agent/models.json
export AZURE_OPENAI_API_KEY="<your key>"
```

If `~/.pi/agent/models.json` already has custom providers, merge the `renci-azure` block instead
of overwriting the file. To point it at your own Azure OpenAI resource, change its `baseUrl` (a
custom provider needs the URL written out) and the model ids to your deployments. The key uses
Pi's standard Azure name, `AZURE_OPENAI_API_KEY`; if you also use Azure OpenAI for other work, set
it per project rather than globally.

Every key the pipeline can use, and which step needs it, is listed in
[`.env.example`](../.env.example) at the repository root.

## Subagents

`.pi/settings.json` declares the pinned `@tintinweb/pi-subagents` package. Pi installs it after project trust is granted. To reconcile packages manually, run:

```bash
pi update --extensions --approve
```

The example custom agent is `.pi/agents/example-reviewer.md`. View and run available agents from `/agents` in an interactive Pi session.
