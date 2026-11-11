"""Garbage collection for a merged KGX graph: drop what can't be right, flag what looks wrong.

Reads `nodes.jsonl` and `edges.jsonl` (ORION's merged output), writes cleaned copies and
`cleaning_report.json`. Records pass through as raw JSON, so every Biolink slot and
`kgbcia_source_records` survives untouched.

An edge is dropped, checked in this order, when it:

- `malformed`: lacks a subject, predicate or object.
- `no_primary_knowledge_source`: has no provenance, so the MCP interface can't explain it.
- `dangling`: names a node that isn't in nodes.jsonl.
- `broken_id`: touches an unresolved node whose id is wrong, not just unknown to NodeNorm: a
  prefix in BROKEN_ID_PREFIXES (`OBA:VT...` trait ids written with the wrong prefix; patients
  from case reports as `phenopacket.store:` nodes).
- `unresolved_both_ends`: both its nodes are ones NodeNorm couldn't resolve (no equivalent
  identifiers), so it can't connect to anything else in the graph.
- `drop_list:<name>`: is listed in `<name>.jsonl` in the drop-lists directory, such as an
  evidence check's output.
- `type_violation:<predicate>-><category>`: a type violation promoted to a drop in
  DROP_TYPE_VIOLATIONS, by predicate and the most specific category of the end that fails
  (`treats` whose object is a Procedure). Promotions are decided from names on real data,
  never from a predicate alone, so an unattended run makes the same decision a reviewer did.

An edge with one unresolved end is kept and flagged `unresolved_node`: the other end is a
resolved gene, protein or disease, and the fact (a gene's variant, a protein's pathway) is real
even though NodeNorm doesn't carry the other id. A node is dropped when it is `orphan`: no kept
edge touches it (`unresolved` in the report when NodeNorm couldn't resolve it either). Edges
kept but worth a look are counted as flags: `unresolved_node`, `self_loop`,
`knowledge_level_not_provided`,
`agent_type_not_provided`, `type_violation` (the predicate's Biolink domain or range doesn't fit
the node categories; see `biolink_types.py`), `rare_category_combination` (the subject's most
specific categories, the predicate and the object's, seen in under RARE_SHARE of that
predicate's edges; a node can have several most specific categories, see
`biolink_types.most_specific`) and
`opposing_predicate` (the pair also carries the other side of an OPPOSING pair, such as `treats`
or a predicate under it against `contraindicated_in`). Type violations are flags, not drops:
on DrugCentral, `treats` edges to a Procedure were wrong but `treats` edges to a Phenomenon
(pain, skin irritation) were real indications, so a drop has to name a predicate and category,
never a predicate alone.

Every kept edge with a flag from LISTED_FLAGS is written to `flagged_edges.jsonl`, keyed like a
drop list (subject, predicate, object, primary knowledge source) with its flags and endpoint
categories, so the flags can be acted on: passed to an evidence check, or copied into a drop
list. The `not_provided` flags are left out; they describe a source, not an edge.

Nothing here is optional. A source that loses most of its edges to unresolved nodes is listed
under `attention` with the CURIE prefixes responsible: the fix is in that source's metadata (an
id pattern or prefix). `unresolved_nodes.tsv` lists every unresolved node with its name, the
edges it took down and the edges kept with a flag, and the sources and predicates of those
edges, most costly first: the identifiers a plan fix needs, not just the prefix.

Drop lists key edges by subject, predicate, object and primary knowledge source, not by edge
id: the merge assigns new uuids on every run, so ids wouldn't match the next build.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from agents.cleaning import biolink_types

EdgeKey = tuple[str, str, str, str]

EXAMPLES_PER_REASON = 5
ATTENTION_SHARE = 0.5  # a source losing at least this share of its edges is listed under attention
RARE_SHARE = 0.001  # a category combination under this share of its predicate's edges is flagged
RARE_MIN_EDGES = 1000  # only for predicates with at least this many kept edges
# each side matches the predicate and every predicate under it
OPPOSING = (("biolink:treats", "biolink:contraindicated_in"),)
# flags written per edge to flagged_edges.jsonl
LISTED_FLAGS = (
    "unresolved_node",
    "self_loop",
    "type_violation",
    "rare_category_combination",
    "opposing_predicate",
)
# Unresolved ids that are wrong rather than missing from NodeNorm: an edge touching one is
# dropped even when its other end resolves. From the Nov 9 graph: TIGA and GWAS Catalog write
# Vertebrate Trait ids as `OBA:VT0001253` (it should be `VT:0001253`), an id in no ontology; the
# phenopacket-store plan makes a node per patient ("individual II-3"), not a biological concept.
BROKEN_ID_PREFIXES = ("OBA:VT", "phenopacket.store:")
# Type violations promoted to drops: (predicate, side that fails) -> the most specific categories
# of that end that make the edge wrong. Decided from names on the Nov 9 graph: DrugCentral
# `treats` edges to "General Anesthesia" (Procedure), "Asthma management" (ClinicalIntervention),
# "relaxation of muscle" (BiologicalProcess), "Diagnostic agents" (ChemicalEntity),
# "Vancomycin-Resistant Enterococci" (OrganismTaxon), or the drug's own synonym (Protein, Drug).
# `treats` -> Phenomenon is deliberately absent: "severe pain" is a real indication that NodeNorm
# labels Phenomenon. Every other violation stays a flag.
DROP_TYPE_VIOLATIONS: dict[tuple[str, str], frozenset[str]] = {
    ("biolink:treats", "range"): frozenset(
        {
            "biolink:Procedure",
            "biolink:ClinicalIntervention",
            "biolink:BiologicalProcess",
            "biolink:ChemicalEntity",
            "biolink:OrganismTaxon",
            "biolink:Protein",
            "biolink:Drug",
        }
    ),
}


@cache
def opposing_sides() -> tuple[tuple[frozenset[str], frozenset[str]], ...]:
    return tuple(
        (biolink_types.predicates_under(a), biolink_types.predicates_under(b)) for a, b in OPPOSING
    )


def in_opposing(predicate: str) -> bool:
    return any(predicate in a or predicate in b for a, b in opposing_sides())


def edge_key(edge: dict) -> EdgeKey:
    """The identity of an edge across builds."""
    return (
        edge.get("subject", ""),
        edge.get("predicate", ""),
        edge.get("object", ""),
        edge.get("primary_knowledge_source", ""),
    )


def load_drop_list(path: Path) -> set[EdgeKey]:
    """Edge keys from a JSON-lines file, one edge per line with the four key fields.

    Other fields on a line (a reason, a score) are allowed and ignored here.
    """
    keys = set()
    with open(path) as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            key = edge_key(record)
            if not all(key):
                raise ValueError(
                    f"{path}:{number}: a drop-list line needs subject, predicate, object and "
                    "primary_knowledge_source"
                )
            keys.add(key)
    return keys


def load_drop_lists(directory: Path) -> dict[str, set[EdgeKey]]:
    """Every `*.jsonl` drop list in a directory, by file stem. A missing directory means none."""
    if not directory.is_dir():
        return {}
    return {path.stem: load_drop_list(path) for path in sorted(directory.glob("*.jsonl"))}


def category_label(categories: tuple[str, ...]) -> str:
    """A node's most specific categories as one key, e.g. 'biolink:Drug + biolink:SmallMolecule'."""
    return " + ".join(categories) or "(none)"


def is_resolved(node: dict) -> bool:
    """Whether NodeNorm resolved the node. ORION leaves equivalent_identifiers empty when not."""
    return bool(node.get("equivalent_identifiers"))


def prefix(curie: str) -> str:
    return curie.split(":", 1)[0] if ":" in curie else "(no prefix)"


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


@dataclass
class Report:
    """What garbage collection did, for cleaning_report.json."""

    settings: dict
    nodes_in: int = 0
    nodes_out: int = 0
    edges_in: int = 0
    edges_out: int = 0
    dropped_edges: Counter = field(default_factory=Counter)
    dropped_edges_by_source: dict = field(default_factory=lambda: defaultdict(Counter))
    unresolved_prefixes_by_source: dict = field(default_factory=lambda: defaultdict(Counter))
    # (source, prefix of the unresolved end) -> edges; an edge with two unresolved ends counts
    # once per end, so this is what each prefix cost each source
    dropped_edges_by_source_prefix: Counter = field(default_factory=Counter)
    dropped_nodes: Counter = field(default_factory=Counter)
    unresolved_nodes_by_prefix: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)
    flags_by_source: dict = field(default_factory=lambda: defaultdict(Counter))
    edges_out_by_source: Counter = field(default_factory=Counter)
    examples: dict = field(default_factory=lambda: defaultdict(list))
    type_violations_by_predicate: Counter = field(default_factory=Counter)
    # predicate -> "range: biolink:Procedure" -> edges, the level a drop has to be decided at
    type_violations_by_category: dict = field(default_factory=lambda: defaultdict(Counter))
    flagged_edges: int = 0
    combinations: Counter = field(default_factory=Counter)
    combination_examples: dict = field(default_factory=dict)
    opposing: dict = field(default_factory=lambda: defaultdict(set))

    def example(self, reason: str, record: dict) -> None:
        if len(self.examples[reason]) < EXAMPLES_PER_REASON:
            self.examples[reason].append(record)

    def attention(self) -> list[dict]:
        """Sources that lost at least ATTENTION_SHARE of their edges, worst first."""
        found = []
        for source, dropped in self.dropped_edges_by_source.items():
            lost = sum(dropped.values())
            total = lost + self.edges_out_by_source[source]
            if total and lost / total >= ATTENTION_SHARE:
                prefixes = self.unresolved_prefixes_by_source.get(source, Counter())
                found.append(
                    {
                        "source": source,
                        "edges_in": total,
                        "edges_out": total - lost,
                        "share_dropped": round(lost / total, 3),
                        "reasons": dict(dropped.most_common()),
                        "unresolved_prefixes": dict(prefixes.most_common(5)),
                    }
                )
        return sorted(found, key=lambda a: (-a["share_dropped"], -a["edges_in"]))

    def rare_combinations(self) -> list[dict]:
        """Category combinations rare for their predicate: a subject or object whose category
        almost never takes this predicate, which is how a wrong NodeNorm category shows up."""
        per_predicate = Counter()
        for (_, predicate, _), n in self.combinations.items():
            per_predicate[predicate] += n
        found = []
        for (s_cats, predicate, o_cats), n in self.combinations.items():
            total = per_predicate[predicate]
            if total >= RARE_MIN_EDGES and n / total < RARE_SHARE:
                found.append(
                    {
                        "subject_categories": list(s_cats),
                        "predicate": predicate,
                        "object_categories": list(o_cats),
                        "edges": n,
                        "share": round(n / total, 6),
                        "example": self.combination_examples[(s_cats, predicate, o_cats)],
                    }
                )
        return sorted(found, key=lambda c: (c["predicate"], c["share"]))

    def opposing_pairs(self) -> list[dict]:
        """Node pairs carrying predicates from both sides of an OPPOSING pair, with the
        predicates found on each side."""
        found = []
        for (subject, obj), predicates in self.opposing.items():
            for side_a, side_b in opposing_sides():
                hits_a, hits_b = predicates & side_a, predicates & side_b
                if hits_a and hits_b:
                    found.append(
                        {
                            "subject": subject,
                            "object": obj,
                            "predicates": sorted(hits_a) + sorted(hits_b),
                        }
                    )
        return sorted(found, key=lambda p: (p["subject"], p["object"]))

    def to_dict(self) -> dict:
        rare = self.rare_combinations()
        opposing = self.opposing_pairs()
        return {
            "settings": self.settings,
            "nodes": {"in": self.nodes_in, "out": self.nodes_out},
            "edges": {"in": self.edges_in, "out": self.edges_out},
            "attention": self.attention(),
            "dropped_edges": dict(self.dropped_edges.most_common()),
            "dropped_edges_by_source": {
                source: dict(counts.most_common())
                for source, counts in sorted(self.dropped_edges_by_source.items())
            },
            "dropped_edges_by_source_prefix": [
                {"source": source, "prefix": prefix, "edges": n}
                for (source, prefix), n in self.dropped_edges_by_source_prefix.most_common()
            ],
            "dropped_nodes": dict(self.dropped_nodes.most_common()),
            "unresolved_nodes_by_prefix": dict(self.unresolved_nodes_by_prefix.most_common()),
            "flags": dict(self.flags.most_common()),
            "flags_by_source": {
                source: dict(counts.most_common())
                for source, counts in sorted(self.flags_by_source.items())
            },
            "edges_out_by_source": dict(self.edges_out_by_source.most_common()),
            "flagged_edges": self.flagged_edges,
            "type_violations_by_predicate": dict(self.type_violations_by_predicate.most_common()),
            "type_violations_by_category": {
                predicate: dict(counts.most_common())
                for predicate, counts in sorted(self.type_violations_by_category.items())
            },
            "rare_category_combinations": rare,
            "opposing_predicates": opposing[: EXAMPLES_PER_REASON * 10],
            "examples": dict(self.examples),
        }


def is_broken_id(curie: str) -> bool:
    """Whether an unresolved id is wrong, not just unknown to NodeNorm (BROKEN_ID_PREFIXES)."""
    return curie.startswith(BROKEN_ID_PREFIXES)


def edge_drop_reason(
    edge: dict,
    known: set[str],
    unresolved: set[str],
    drop_lists: dict[str, set[EdgeKey]],
) -> str | None:
    """Why this edge is dropped, or None to keep it.

    One unresolved end keeps the edge (it is flagged `unresolved_node` later); both ends, or an
    end with a broken id, drop it.
    """
    subject, predicate, obj = edge.get("subject"), edge.get("predicate"), edge.get("object")
    if not (subject and predicate and obj):
        return "malformed"
    if not edge.get("primary_knowledge_source"):
        return "no_primary_knowledge_source"
    if subject not in known or obj not in known:
        return "dangling"
    ends = [end for end in (subject, obj) if end in unresolved]
    if any(is_broken_id(end) for end in ends):
        return "broken_id"
    if subject in unresolved and obj in unresolved:
        return "unresolved_both_ends"
    if drop_lists:
        key = edge_key(edge)
        for name, keys in drop_lists.items():
            if key in keys:
                return f"drop_list:{name}"
    return None


def promoted_type_violation(
    predicate: str,
    subject_categories: tuple[str, ...],
    object_categories: tuple[str, ...],
    subject_specific: tuple[str, ...],
    object_specific: tuple[str, ...],
) -> str | None:
    """The drop reason when a type violation is in DROP_TYPE_VIOLATIONS, else None.

    The violation is tested on the full category lists (ancestors included); the promotion on
    the most specific categories of the end that fails, any of which may match.
    """
    side = biolink_types.violation(predicate, subject_categories, object_categories)
    if not side:
        return None
    promoted = DROP_TYPE_VIOLATIONS.get((predicate, side))
    if not promoted:
        return None
    failing = subject_specific if side == "domain" else object_specific
    hits = sorted(set(failing) & promoted)
    return f"type_violation:{predicate}->{hits[0]}" if hits else None


def edge_flags(
    edge: dict, subject_categories=(), object_categories=(), unresolved: set[str] = frozenset()
) -> Iterable[str]:
    """Things worth a look on an edge that is kept."""
    if edge["subject"] in unresolved or edge["object"] in unresolved:
        yield "unresolved_node"
    if edge["subject"] == edge["object"]:
        yield "self_loop"
    if edge.get("knowledge_level", "not_provided") == "not_provided":
        yield "knowledge_level_not_provided"
    if edge.get("agent_type", "not_provided") == "not_provided":
        yield "agent_type_not_provided"
    if biolink_types.violation(edge["predicate"], subject_categories, object_categories):
        yield "type_violation"


def collect(input_dir: Path, output_dir: Path, drop_lists_dir: Path | None = None) -> Report:
    """Clean input_dir's nodes.jsonl and edges.jsonl into output_dir, and return the report.

    Drop lists are read from drop_lists_dir, by default `output_dir/drop_lists`. Four streaming
    passes, so only node ids, categories and the unresolved nodes' names are held in memory:
    nodes (which ids exist, which are unresolved, their categories), edges (filter, including
    promoted type violations; count category combinations and opposing predicates; note what
    each unresolved node cost, then write unresolved_nodes.tsv), kept edges again (flag, now
    that rare combinations and opposing pairs are known, and write flagged_edges.jsonl), nodes
    again (write the ones still in use).
    """
    nodes_path, edges_path = input_dir / "nodes.jsonl", input_dir / "edges.jsonl"
    for path in (nodes_path, edges_path):
        if not path.exists():
            raise FileNotFoundError(f"no {path.name} in {input_dir}")
    drop_lists_dir = drop_lists_dir or output_dir / "drop_lists"
    drop_lists = load_drop_lists(drop_lists_dir)
    report = Report(
        settings={
            "input": str(input_dir.resolve()),
            "drop_lists_dir": str(drop_lists_dir),
            "drop_lists": {name: len(keys) for name, keys in drop_lists.items()},
        }
    )

    known: set[str] = set()
    unresolved: dict[str, str] = {}  # id -> name, for unresolved_nodes.tsv
    # categories per node, as an index into the few distinct category lists the graph has
    profiles: list[tuple[str, ...]] = []
    profile_of: dict[tuple[str, ...], int] = {}
    categories_of: dict[str, int] = {}
    specific: list[tuple[str, ...]] = []  # each profile's most specific categories
    for node in read_jsonl(nodes_path):
        report.nodes_in += 1
        node_id = node.get("id")
        if not node_id:
            continue
        known.add(node_id)
        if not is_resolved(node):
            unresolved[node_id] = str(node.get("name") or "")
            report.unresolved_nodes_by_prefix[prefix(node_id)] += 1
        profile = tuple(node.get("category") or [])
        if profile not in profile_of:
            profile_of[profile] = len(profiles)
            profiles.append(profile)
            specific.append(biolink_types.most_specific(profile))
        categories_of[node_id] = profile_of[profile]

    output_dir.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    unresolved_ids = set(unresolved)
    # what each unresolved node cost: edges lost, edges kept with a flag, and the (source,
    # predicate) pairs of the edges it lost
    lost: Counter = Counter()
    kept: Counter = Counter()
    lost_from: dict[str, set[tuple[str, str]]] = defaultdict(set)
    with open(output_dir / "edges.jsonl", "w") as out:
        for edge in read_jsonl(edges_path):
            report.edges_in += 1
            source = edge.get("primary_knowledge_source") or "(none)"
            reason = edge_drop_reason(edge, known, unresolved_ids, drop_lists)
            if reason is None:
                s_profile, o_profile = categories_of[edge["subject"]], categories_of[edge["object"]]
                s_spec, o_spec = specific[s_profile], specific[o_profile]
                reason = promoted_type_violation(
                    edge["predicate"], profiles[s_profile], profiles[o_profile], s_spec, o_spec
                )
            if reason:
                report.dropped_edges[reason] += 1
                report.dropped_edges_by_source[source][reason] += 1
                report.example(reason, edge)
                if reason in ("unresolved_both_ends", "broken_id"):
                    for end in {edge["subject"], edge["object"]} & unresolved_ids:
                        report.unresolved_prefixes_by_source[source][prefix(end)] += 1
                        report.dropped_edges_by_source_prefix[(source, prefix(end))] += 1
                        lost[end] += 1
                        lost_from[end].add((source, edge["predicate"]))
                continue
            for end in {edge["subject"], edge["object"]} & unresolved_ids:
                kept[end] += 1
            combination = (s_spec, edge["predicate"], o_spec)
            report.combinations[combination] += 1
            report.combination_examples.setdefault(combination, edge_key(edge)[:3])
            if in_opposing(edge["predicate"]):
                report.opposing[(edge["subject"], edge["object"])].add(edge["predicate"])
            used.add(edge["subject"])
            used.add(edge["object"])
            report.edges_out += 1
            report.edges_out_by_source[source] += 1
            out.write(json.dumps(edge) + "\n")

    with open(output_dir / "unresolved_nodes.tsv", "w") as out:
        out.write("node\tprefix\tname\tdropped_edges\tkept_edges\tsources\tpredicates\n")
        for node_id in sorted(unresolved, key=lambda n: (-lost[n], n)):
            sources = ";".join(sorted({s for s, _ in lost_from.get(node_id, ())}))
            predicates = ";".join(sorted({p for _, p in lost_from.get(node_id, ())}))
            name = unresolved[node_id].replace("\t", " ").replace("\n", " ")
            out.write(
                f"{node_id}\t{prefix(node_id)}\t{name}\t{lost[node_id]}\t{kept[node_id]}\t"
                f"{sources}\t{predicates}\n"
            )
    del lost_from

    rare = {
        (tuple(c["subject_categories"]), c["predicate"], tuple(c["object_categories"]))
        for c in report.rare_combinations()
    }
    opposing = {(p["subject"], p["object"]): set(p["predicates"]) for p in report.opposing_pairs()}
    with open(output_dir / "flagged_edges.jsonl", "w") as out:
        for edge in read_jsonl(output_dir / "edges.jsonl"):
            source = edge["primary_knowledge_source"]
            s_profile = categories_of[edge["subject"]]
            o_profile = categories_of[edge["object"]]
            s_cats, o_cats = profiles[s_profile], profiles[o_profile]
            s_spec, o_spec = specific[s_profile], specific[o_profile]
            flags = list(edge_flags(edge, s_cats, o_cats, unresolved_ids))
            if (s_spec, edge["predicate"], o_spec) in rare:
                flags.append("rare_category_combination")
            if edge["predicate"] in opposing.get((edge["subject"], edge["object"]), ()):
                flags.append("opposing_predicate")
            for flag in flags:
                report.flags[flag] += 1
                report.flags_by_source[source][flag] += 1
            listed = [flag for flag in flags if flag in LISTED_FLAGS]
            if not listed:
                continue
            line = dict(
                zip(
                    ("subject", "predicate", "object", "primary_knowledge_source"),
                    edge_key(edge),
                    strict=True,
                )
            )
            line.update(
                flags=listed, subject_categories=list(s_spec), object_categories=list(o_spec)
            )
            if "type_violation" in listed:
                side = biolink_types.violation(edge["predicate"], s_cats, o_cats)
                line["violates"] = side
                report.type_violations_by_predicate[edge["predicate"]] += 1
                report.type_violations_by_category[edge["predicate"]][
                    f"{side}: {category_label(s_spec if side == 'domain' else o_spec)}"
                ] += 1
                report.example("type_violation", edge)
            report.flagged_edges += 1
            out.write(json.dumps(line) + "\n")

    with open(output_dir / "nodes.jsonl", "w") as out:
        for node in read_jsonl(nodes_path):
            node_id = node.get("id")
            if not node_id:
                reason = "malformed"
            elif node_id not in used:
                reason = "unresolved" if node_id in unresolved else "orphan"
            else:
                reason = None
            if reason:
                report.dropped_nodes[reason] += 1
                report.example(f"node:{reason}", node)
                continue
            report.nodes_out += 1
            out.write(json.dumps(node) + "\n")

    (output_dir / "cleaning_report.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return report


def summary(report: Report) -> str:
    """A few lines for the terminal."""
    lines = [
        f"Nodes: {report.nodes_in:,} in, {report.nodes_out:,} out",
        f"Edges: {report.edges_in:,} in, {report.edges_out:,} out",
    ]
    if report.settings["drop_lists"]:
        lists = ", ".join(f"{k} ({v:,})" for k, v in report.settings["drop_lists"].items())
        lines.append(f"Drop lists applied: {lists}")
    for title, counts in (
        ("Dropped edges", report.dropped_edges),
        ("Dropped nodes", report.dropped_nodes),
        ("Flagged edges (kept)", report.flags),
    ):
        if counts:
            lines.append(f"{title}:")
            lines += [f"  {reason}: {n:,}" for reason, n in counts.most_common()]
    attention = report.attention()
    if attention:
        lines.append("Needs attention (fix in the source's metadata, then rebuild):")
        for a in attention:
            prefixes = ", ".join(a["unresolved_prefixes"]) or "-"
            lines.append(
                f"  {a['source']}: {a['share_dropped']:.0%} of {a['edges_in']:,} edges dropped; "
                f"unresolved prefixes: {prefixes}"
            )
    return "\n".join(lines)
