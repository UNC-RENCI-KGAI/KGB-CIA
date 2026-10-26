# Augmentation agent

**Team:** @UNC-RENCI-KGAI/augmentation · **Existing asset:** PubMed; Bagel / LitCoin for text

Enrich the baseline graph with extra datasets and PubMed evidence for or against edges.

## Initial plan: PubMed evidence for existing edges

1. Read the JSON file of the baseline graph produced by the filter stage.
2. Find the node pairs joined by one or more edges.
3. Query PubMed for each pair of entities and retrieve the matching abstracts with their PMIDs.
4. From the abstracts, extract any evidence for or against the existing edges between that pair.
5. Mark each edge with one of four evidence labels:

   | Label | Meaning |
   | --- | --- |
   | confirmed | An abstract explicitly supports the edge |
   | contradicted | An abstract explicitly refutes the edge |
   | ambiguous | Abstracts discuss the pair, but the evidence is unclear or mixed |
   | no evidence | No abstract was found, or none bears on the edge |

The PMIDs behind a label go in the edge's `Provenance.evidence`, added to what is there rather
than replacing it. Where the label itself lives (an `Edge.properties` key or a new schema field)
is still to be decided.

## Run it alone

```bash
uv run kgb-augment --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`.

- **Input:** a JSON file of the filtered baseline graph (a `Graph`).
- **Output:** the same graph with every edge labeled and its supporting PMIDs recorded.

Describe the exact files and paths here once decided.

## Status

See the tracking issue for this team on GitHub. Replace this section with what works, what
doesn't, and how to try it.
