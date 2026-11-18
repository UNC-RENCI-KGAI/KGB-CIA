# KGB-CIA: Knowledge Graph Based Creation of Informed Agents

KGB-CIA is a pipeline of AI agents. It reads raw biomedical data (drug–target data from
[Pharos](https://pharos.nih.gov/) / TCRD and the knowledge bases behind it) and works out what
the data means and how it fits together. It then builds a [Biolink](https://biolink.github.io/biolink-model/)
knowledge graph and lets other agents query that graph. No step requires a person to hand-map
columns or write transform code. A person steps in once, and only briefly, to say which part of
the graph they want, and every question has a working default.

This is a team entry for the **Bio-Agent Knowledge Graph Construction Challenge** (Structured
Data Integration and Link Prediction track), submitted Nov 17, 2026.

**[▶ Watch the demo](https://github.com/UNC-RENCI-KGAI/KGB-CIA/releases/download/demo/kgb-cia-demo.mp4)**
(5 minutes): one complete run on a small AWS cloud server, from three sources' metadata
(ClinVar, Monarch and eRAM) to a cardiomyopathy subgraph, its PubMed evidence, the evidence
check, and answers through the MCP server. That run is a smaller build than the submitted graph
described below.

## What it produces

The submitted graph comes from the Nov 9, 2026 build: 22 sources merged into one graph, citing
45 primary knowledge sources.

- **943,412 nodes and 11,710,097 edges:** 11,701,010 after cleaning, plus 9,087 edges
  augmentation proposed from PubMed. Every node ID is normalized with
  [NodeNorm](https://github.com/TranslatorSRI/NodeNormalization).
- **Biolink provenance on every edge:** `primary_knowledge_source`, `knowledge_level`,
  `agent_type`, and publications where the source gives them. Each edge also points back to
  the source file, columns and record it came from, so an agent can answer "why does A connect
  to B?".
- **Pruning with reasons.** Cleaning removed 298,421 edges that can't be right, for example
  edges with both ends unresolved, broken IDs, or `treats` pointing at a procedure. Each removed
  edge is listed with its reason in `pruned_edges.tsv`. Cleaning also flagged 2,090,958 kept
  edges as suspicious and wrote each flag's reason onto the edge.
- **Evidence from the literature.** One step adds PubMed citations to edges where abstracts
  support them. Another, the evidence check, drops or flags an edge whose cited abstract says
  otherwise.

What every stage produced on the way to the submitted graph (the metadata, the agents'
decisions, the build records, what cleaning removed and why, the evaluation) is in the
[submission folder](https://kgb-cia-972034964799.s3.us-east-2.amazonaws.com/submission-2026-11-17/README.md),
one folder per stage. The graph itself is served read-only through the MCP server (see
[Querying the graph](#querying-the-graph)); its files aren't redistributed, because some sources'
licenses don't allow it. What the submitted graph is known to be missing is in
[`docs/known_limitations.md`](docs/known_limitations.md).

## Pipeline

Eight stages, in this order. `uv run kgb-pipeline` runs them all; `make demo` prints what each
does without running anything. Each stage's README says how to run it on its own.

| Stage | Command | What it does |
| --- | --- | --- |
| 1. [Metadata](agents/metadata/) | the [`croissant-semantics`](.agents/skills/metadata/) skill | Adds Biolink meaning to each source's [Croissant](https://mlcommons.org/croissant/) metadata, one source at a time. (`kgb-metadata` is not implemented yet.) |
| 2. [Transformation](agents/transformation/) | `kgb-transform` | Generates an [ORION](https://github.com/RobokopU24/ORION) parser per source, builds each one, and merges them into one graph. |
| 3. [Cleaning](agents/cleaning/) | `kgb-clean` | Drops edges that can't be right, flags suspicious ones, and writes the reason onto each edge. |
| 4. [Filter](agents/filter/) | `kgb-filter` | Gives a plain-language overview of the graph, then cuts the subgraph the user asks for. With no request, it keeps the whole graph. |
| 5. [Augmentation](agents/augmentation/) | `kgb-augment` | Adds PubMed evidence to edges, and proposes more specific edges that the abstracts support. |
| 6. [Evidence check](agents/cleaning/) | `kgb-evidence` | Asks a model (Jev) whether each edge's cited abstract supports it, and writes drop and flag lists. Skipped without a TypeSafe key. |
| 7. [Final cleaning](agents/cleaning/) | `kgb-clean` | Applies the evidence check's drop and flag lists. |
| 8. [MCP interface](agents/mcp_interface/) | `kgb-mcp` | A read-only [MCP](https://modelcontextprotocol.io/) server over a Neo4j copy of the graph, for any MCP client. |

Cleaning runs before the filter and augmentation, so the paid steps never spend money on edges
cleaning would drop. The filter runs before augmentation, so the paid steps only see what the
user asked for. Link prediction against the Pharos baseline ([`evaluation/`](evaluation/)) runs
outside the pipeline, and shared code (record schema, NodeNorm client, PubMed cache) is in
[`shared/`](shared/).

## Quick start

```bash
git clone https://github.com/UNC-RENCI-KGAI/KGB-CIA.git
cd KGB-CIA
make setup        # installs everything into .venv, including Python 3.12 if you lack it
make test         # lint + tests (offline, under a minute; see tests/README.md)
make demo         # dry run: prints what each stage does, runs nothing
```

**The one thing to install first is [uv](https://docs.astral.sh/uv/getting-started/installation/)**
(`curl -LsSf https://astral.sh/uv/install.sh | sh`, or `brew install uv`). `make setup` uses it
to fetch Python 3.12+ if needed and to install every Python dependency from the lock file. Each
skill's scripts declare their own dependencies, which uv installs the first time a script runs.

Some steps need more, and only those steps:

| Need | For |
| --- | --- |
| [Docker](https://docs.docker.com/get-docker/) | Building the Neo4j copy of the graph for the MCP server (`agents/mcp_interface/deploy/`), and `make test-neo4j` |
| [Helm](https://helm.sh/docs/intro/install/) and a Kubernetes cluster | Deploying the MCP server with its chart. Not needed to run it locally. |
| Disk for the merge: about 500 bytes per merged edge, twice (about 12 GB for the submitted graph), plus the per-source graphs | Merging every source in `kgb-transform` |
| API keys | The paid model steps; see below |

## API keys

You need no key to build, clean or filter the graph, or to serve it over MCP. Keys are only for
the two steps that call a paid model:

- **Augmentation** needs a language model: an Anthropic key for Claude models, or an Azure
  OpenAI key and endpoint.
- **The evidence check** needs a TypeSafe AI key for its Jev model.
- **PubMed** works without a key; a free NCBI key makes searches faster (10 requests a second
  instead of 3).

[`.env.example`](.env.example) lists every variable and the step that reads it. Copy it to `.env`
(gitignored), fill in what you have, and run with `uv run --env-file .env <command>`. The scripts
only read environment variables, so exporting them from your shell or a secrets manager works
too. To use the models from the Pi agent harness, see [`.pi/README.md`](.pi/README.md).

## Querying the graph

The MCP server is on PyPI as [`kgbcia-mcp`](https://pypi.org/project/kgbcia-mcp/), so any MCP
client (Claude Code, Pi, or another) can run it with `uvx kgbcia-mcp serve`, no checkout needed;
it also needs the graph's Neo4j address and its artifacts URL and checksum, which the MCP README
shows how to set.
It searches nodes, explores neighbors and paths, explains an edge down to the source record it
came from, and says when cleaning flagged an edge, and why.
[`agents/mcp_interface/README.md`](agents/mcp_interface/README.md) has the client configuration,
and how to load the graph into a Neo4j of your own.

## Agent skills

The agents' skills live in [`.agents/skills/`](.agents/skills/), one folder per stage. Each
skill is a `SKILL.md` that an agent follows, plus its scripts, references and tests. Pi reads
them directly. For Claude Code, run `python3 scripts/sync_claude_skills.py --prune`, which links
them into `.claude/skills/`. See [the skills guide](.agents/skills/README.md).

## Data

No data lives in this repo. Source downloads, caches and generated graphs go under `data/`,
which git ignores (see [`data/README.md`](data/README.md)). Each source keeps the license of its
upstream knowledge base.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md) covers branches, reviews and conventions. Coding agents
should read [`AGENTS.md`](AGENTS.md) first.

## Team

Built by Ashok Krishnamurthy, Chris Bizon, Cheng-Han Chung, Gaurav
Vaidya, Hina Shah, Hong Yi, James McManus, Jason Reilly, Kathleen Carter, Kushal Koirala, Matt
Satusky, Olawumi Olasunkanmi, Sahil Kapadia, Suparna Goswami, Swaroopa Dola, Yaphet Kebede and
Yogesh Kale. The original plan, with Chris Bizon's guidance, is in
[`docs/agent_plan.md`](docs/agent_plan.md).

## License

MIT. See [`LICENSE`](LICENSE).
