# Agent Plan: Subagents for the KG Construction Challenge

**Sources.** The kickoff meeting of Oct 6, 2026 (team meeting notes, not in this repo) and Chris Bizon's nine comments of Oct 10, 2026 on the simplified meeting summary (shared with the team on Slack). Chris's guidance is quoted or closely paraphrased and marked "Chris:". Everything else is from the meeting. Nothing here comes from outside those two sources, except acronym expansions in parentheses.

## Design assumptions

- **Target graph:** a Biolink LPG (labeled property graph). Chris: "My assumption: we are going to target biolink LPG. So the point of the agent here is to figure out how to do that."
- **Agents decide, code transforms.** Chris: "The actual transform should be done with code." Meeting: the builder writes code rather than doing everything in one shot, so the result is reproducible.
- **Normalization:** everything runs through NodeNorm, using the API we already host. Biolink is fixed and not user-selectable.
- **Strong defaults everywhere.** Users can override at the filter step, but a user who knows nothing about ontologies must be able to say "go with your default".
- **Evaluation is outside the agent.** The team runs GNN link prediction on the final graph against the Pharos baseline; the agent does not.

## Pipeline

```
Pharos / TCRD files
        |
        v
1. Metadata agent (RoboCroissant)      -> Croissant metadata per file and column;
        |                                  optionally cross-column types ("these columns are properties of that gene")
        v
2. Transformation agent                -> decides which columns map to which Biolink class and relationship,
        |                                  writes and runs code; full transform as much as possible
        v
3. Filter and integration agent        <- user back-and-forth, strong defaults
        |
        v
4. Augmentation agent                  -> mostly PubMed; text handling via Bagel / LitCoin
        |
        v
5. Cleaning ("janitor") agent          -> conflict resolution and harmonization; no existing asset, needs a plan
        |
        v
6. Graph access interface (MCP)        -> lets any local agent use the graph we built; modeled on the OKN MCP
        |
        v
Evaluation pipeline (not an agent)     -> GNN link prediction vs. Pharos baseline
```

Chris's comments add one component to the meeting's five: an interface at the end so other agents can consume the graph. The rest of the sequence is unchanged.

## Subagents

### 1. Metadata agent

- **Purpose:** describe each source file and its columns as Croissant metadata. Purely descriptive; builds no graph.
- **Input:** a data source to ingest (Pharos site, TCRD downloads, later any additional source).
- **Output:** Croissant metadata files, one per source, listing every downloadable file and the meaning of each column.
- **Existing asset:** RoboCroissant, the work Jason and Kathleen have done. Chris: "Robocroissant. Not a lot to do here."
- **Possible extension (Chris):** "we could augment robocroissant with some of the transformation agent stuff. For instance, there are formations in croissant that you can use to define types that cross different columns in the dataset saying 'all these columns are properties of that gene'. This is something I'd like to have in RC anyway." If adopted, part of the column-to-entity grouping moves from agent 2 into agent 1.
- **Status:** exists; small extension optional.

### 2. Transformation agent

- **Purpose:** figure out how to turn the described files into a Biolink LPG, then do it with code. Chris: "Which set of columns maps to a gene, whats the relationships."
- **Input:** Croissant metadata from agent 1, the Biolink model, and built-in rules such as "don't include any binds edges for affinities less than X".
- **Process:** recognize which tables express relationships (meeting example: a ligand column and a protein column mean "binds"), print a summary of the planned graph (node types, edge types) and state that Biolink will be used, then write the transform code and execute it.
- **Output:** a harmonized set of subject, predicate, object triples with NodeNorm-normalized nodes. Chris: "This is a full transform as much as possible," meaning the agent should convert everything it can rather than a chosen subset; selection happens in agent 3.
- **Existing asset:** none for the agent itself. Chris: "We have nothing on this agent wise, we could adapt parts of ORION for the deterministic stuff." The meeting agreed that simply wrapping ORION as the whole answer would not score points, so ORION pieces serve only the deterministic transform code the agent writes or calls.
- **Status:** needs to be built; largest piece of new work alongside agent 5.

### 3. Filter and integration agent

- **Purpose:** apply user choices and merge the triples into one connected graph.
- **Input:** the full triple set from agent 2.
- **Process:** defaults for affinity cutoffs, weak versus strong binding bins, and which edge types to keep; integrate so that, for example, one ligand binding three proteins becomes one node with three edges. Chris: "This feels to me like it will involve a bit of back and forth with the user - though as noted we want strong defaults."
- **Output:** the baseline knowledge graph. Per Kathleen, keep this baseline distinct from anything pulled in later to answer a specific question.
- **Existing asset:** none named.
- **Status:** needs a design for the user dialogue (what is asked, in what order, with what defaults).

### 4. Augmentation agent

- **Purpose:** enrich the baseline graph with new datasets and literature support.
- **Input:** the baseline graph.
- **Process:** pull in additional datasets, possibly by sending them back through agents 1 to 3; query PubMed for context around each edge and for support or refutation. Chris: "This seems like a lot of fun - mostly pubmed."
- **Existing assets (Chris):** "If relevant we could use some of the Bagel / lit coin stuff for dealing with text."
- **Open from the meeting:** whether pre-built ROBOKOP graphs may be linked in, and whether AWS registry datasets should be the preferred extra sources to use the AWS credits.
- **Status:** PubMed path has reusable pieces; dataset path reuses agents 1 to 3.

### 5. Cleaning agent ("janitor")

- **Purpose:** final cleaning and harmonization, resolving conflicts between sources once everything is merged. Maps to the accuracy and pruning criterion.
- **Input:** the augmented graph.
- **Output:** the final graph, possibly in several versions.
- **Existing asset:** none. Chris: "We don't have anything like this. Need a plan."
- **Open from the meeting and chat:** whether an evaluating agent feeds back into construction as a loop (Hina's question, Kathleen's feedback-loop view).
- **Status:** unplanned; needs an owner and a design before anything else.

### 6. Graph access interface (new, from Chris)

- **Purpose:** make the finished graph usable by whatever agent the user already runs. Chris: "I think we should also have an agent here - or more specifically an interface for agents - something that looks a lot like the okn mcp so that your local agent (whatever it is) can effectively use the graph we made."
- **Input:** the final graph from agent 5.
- **Output:** an MCP (Model Context Protocol) style interface exposing the graph to external agents.
- **Why it matters:** the meeting agreed the judges expect an agent that both builds and explores the graph, and that this needs to be stated explicitly. This component is how exploration is delivered without building a second full agent.
- **Existing asset:** the OKN MCP, as a model to copy.
- **Status:** needs to be built; pattern exists.

### Evaluation pipeline (not an agent)

- Run GNN, GCN, or graph-embedding link prediction on the final graph and on the Pharos baseline, and report the comparison. Hina asked whether GPUs should be used when available.
- Blocked on the baseline definition; see open questions.

## Status summary

| Component | Existing asset | Status per Chris and meeting |
| --- | --- | --- |
| 1. Metadata agent | RoboCroissant | Exists; optional cross-column typing extension |
| 2. Transformation agent | ORION (deterministic parts only) | Nothing agent-wise; build |
| 3. Filter and integration agent | none | Design the user dialogue and defaults |
| 4. Augmentation agent | Bagel / LitCoin for text; PubMed | Reusable pieces; build the agent |
| 5. Cleaning agent | none | No plan yet; needs one first |
| 6. Graph access interface | OKN MCP as pattern | Build |
| Evaluation pipeline | none named | Blocked on baseline definition |

## Open questions carried forward

- Which Pharos artifact is the baseline KG for the comparison. An email to Ben requesting the host's list of accepted baselines is in progress.
- Whether ROBOKOP graphs may be linked in during augmentation.
- Where the pipeline runs; nothing has to run on AWS, and the credits are a resource.
- Who owns each of the six components. Agents 2 and 5 have no existing code and need owners first.
