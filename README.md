# KGB-CIA: Knowledge Graph Based Creation of Informed Agents

KGB-CIA is a pipeline of AI agents. It reads raw biomedical data (drug–target data from
[Pharos](https://pharos.nih.gov/) / TCRD and the knowledge bases behind it) and works out what
the data means and how it fits together. It then builds a [Biolink](https://biolink.github.io/biolink-model/)
knowledge graph and lets other agents query that graph. No step requires a person to hand-map
columns or write transform code. A person steps in once, and only briefly, to say which part of
the graph they want, and every question has a working default.

This is a team entry for the **Bio-Agent Knowledge Graph Construction Challenge** (Structured
Data Integration and Link Prediction track), submitted Nov 17, 2026.

## What it produces

The submitted graph comes from the Nov 9, 2026 build of 26 sources (45 primary knowledge
sources):

- **943,412 nodes and 11,701,010 edges** after cleaning. Every node ID is normalized with
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

The graph files are too large for git and are distributed separately.
<!-- TODO before merge: link to where the submitted graph can be downloaded. -->
What the submitted graph is known to be missing is in
[`docs/known_limitations.md`](docs/known_limitations.md).

## Pipeline

```
Pharos / TCRD files
        |
        v
1. Metadata agent        agents/metadata/        Croissant metadata per file (RoboCroissant), plus Biolink semantics
        |
        v
2. Transformation agent  agents/transformation/  one ORION parser per source, built and NodeNorm'd, then merged into one graph
        |
        v
3. Cleaning agent        agents/cleaning/        drops edges that can't be right; flags suspicious ones, with the reason on the edge
        |
        v
4. Filter                agents/filter/          the subgraph the user asks for; with no request, the whole graph
        |
        v
5. Augmentation agent    agents/augmentation/    PubMed evidence for edges, and more specific edges it supports
        |
        v
6. Final cleaning        agents/cleaning/        the same kgb-clean, applying the evidence check's drop and flag lists
        |
        v
7. MCP interface         agents/mcp_interface/   lets any local agent query the graph (OKN MCP pattern)

Evaluation pipeline      evaluation/             GNN link prediction vs. Pharos baseline (not an agent)
Shared code              shared/                 record schema, NodeNorm client, PubMed cache, pipeline runner
```

| Stage | Command | What it does |
| --- | --- | --- |
| 1. Metadata | the [`croissant-semantics`](.agents/skills/metadata/) skill | Adds Biolink semantics to each source's [Croissant](https://mlcommons.org/croissant/) metadata, one source at a time. (`kgb-metadata` is not implemented yet.) |
| 2. Transformation | `kgb-transform` | Generates an [ORION](https://github.com/RobokopU24/ORION) parser per source, builds each one, and merges them into one graph. |
| 3. Cleaning | `kgb-clean` | Drops edges that can't be right, flags suspicious ones, and writes the reason onto each edge. |
| 4. Filter | `kgb-filter` | Gives a plain-language overview of the graph, then cuts the subgraph the user asks for. With no request, it keeps the whole graph. |
| 5. Augmentation | `kgb-augment` | Adds PubMed evidence to edges, and proposes more specific edges that the abstracts support. |
| 6. Final cleaning | `kgb-clean` | Applies the evidence check's drop and flag lists. |
| 7. MCP interface | `kgb-mcp` | A read-only [MCP](https://modelcontextprotocol.io/) server over a Neo4j copy of the graph, for any MCP client. |
| Evaluation | not a stage | Link prediction against the Pharos baseline, run outside the agents. |

Each stage's README, in its directory under `agents/`, says how to run the stage on its own.
Cleaning runs before the filter and augmentation, so the paid steps never spend money on edges
cleaning would drop. The filter runs before augmentation, so those steps only see what the user
asked for. Cleaning then runs again at the end.

## Quick start

```bash
git clone https://github.com/UNC-RENCI-KGAI/KGB-CIA.git
cd KGB-CIA
make setup        # creates .venv with uv and installs the package in editable mode
make test         # lint + tests (offline, about 15 s; see tests/README.md)
make demo         # dry run: prints what each stage does, runs nothing
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). `make setup` falls back to `pip` if
uv is not installed.

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

[`agents/mcp_interface/README.md`](agents/mcp_interface/README.md) explains how to load the
graph into Neo4j and connect an MCP client (Claude Code, Pi, or any other) to the server. The
server can search nodes, explore neighbors and paths, explain an edge down to the source record
it came from, and tell the user when cleaning flagged an edge, and why.

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
