#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pyyaml", "bioregistry", "truststore"]
# ///
"""Look up vocabulary terms: Biolink classes and slots, identifier prefixes, and ontology terms.

    vocab.py class Gene                 # a Biolink class: definition, parents, id prefixes, slots
    vocab.py slot has_evidence          # a Biolink slot: definition, domain, range
    vocab.py search "evidence"          # Biolink classes and slots whose name or definition matches
    vocab.py search RO:0002331          # ... or that are mapped to this term (GO's RO relations)
    vocab.py prefix FB                  # a CURIE prefix: Biolink's expansion, Bioregistry record
    vocab.py identify FBgn0031086 12345 # which registered prefixes a bare id could belong to
    vocab.py associations --subject ChemicalEntity --object Disease  # association classes that fit
    vocab.py term ECO:0000315           # an ontology term by CURIE (exact)
    vocab.py infores ChEMBL             # infores ids for a source name (knowledge_source)
    vocab.py unit "per molar second"    # QUDT units by label, UCUM code or symbol
    vocab.py ols "evidence used in automatic assertion" [--ontology eco]
                                        # free-text search of OBO ontologies (EBI OLS);
                                        # exact labels are often missed: when you know the id, use
                                        # term

Biolink is pinned to the version in BIOLINK_VERSION; files are cached in ~/.cache/croissant-
semantics."""

import argparse
import json
import re
import sys
from pathlib import Path

import requests
import truststore
import yaml

truststore.inject_into_ssl()  # the OS certificate store, as in sample.py

BIOLINK_VERSION = "v4.4.6"
BIOLINK_RAW = f"https://raw.githubusercontent.com/biolink/biolink-model/{BIOLINK_VERSION}"
CACHE = Path.home() / ".cache" / "croissant-semantics" / BIOLINK_VERSION


def _cached(name: str, url: str) -> str:
    path = CACHE / name
    if not path.exists():
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text)
    return path.read_text()


def biolink() -> dict:
    return yaml.safe_load(_cached("biolink-model.yaml", f"{BIOLINK_RAW}/biolink-model.yaml"))


INFORES_URL = "https://raw.githubusercontent.com/biolink/information-resource-registry/main/infores_catalog.yaml"
INFORES_MAX_AGE_DAYS = 30


def infores() -> dict[str, dict]:
    """The infores catalog: id -> entry. Not versioned with Biolink; refreshed when the cached copy
    is 30 days old."""
    import time

    path = CACHE.parent / "infores_catalog.yaml"
    if not path.exists() or time.time() - path.stat().st_mtime > INFORES_MAX_AGE_DAYS * 86400:
        r = requests.get(INFORES_URL, timeout=60)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text)
    return {e["id"]: e for e in yaml.safe_load(path.read_text())["information_resources"]}


def biolink_prefixes() -> dict:
    return json.loads(
        _cached("prefix-map.json", f"{BIOLINK_RAW}/project/prefixmap/biolink_model_prefix_map.json")
    )


def to_key(name: str) -> str:
    """Biolink YAML keys have spaces: 'GeneToDiseaseAssociation' -> 'gene to disease association',
    'RNAProduct' -> 'rna product' (matched case-insensitively)."""
    name = name.removeprefix("biolink:")
    if " " in name or name.islower():
        return name.replace("_", " ").lower()
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name).lower()


def curie_class(key: str) -> str:
    return "biolink:" + "".join(w[:1].upper() + w[1:] for w in key.split())


def curie_slot(key: str) -> str:
    return "biolink:" + key.replace(" ", "_")


def find(model: dict, section: str, name: str) -> tuple[str, dict] | None:
    key = to_key(name)
    items = model[section]
    for k, v in items.items():
        if k.lower() == key or k.lower().replace(" ", "") == key.replace(" ", ""):
            return k, v
    for k, v in items.items():
        if key in [a.lower() for a in (v.get("aliases") or [])]:
            print(f"(resolved {name} through an alias of {k})", file=sys.stderr)
            return k, v
    return None


def suggest(model: dict, section: str, name: str, fmt) -> str:
    import difflib

    key = to_key(name)
    words = set(key.split())
    names = list(model[section])
    close = difflib.get_close_matches(key, names, n=6, cutoff=0.5)
    overlap = sorted(names, key=lambda k: -len(words & set(k.split())))[:6]
    picks = list(dict.fromkeys(close + [k for k in overlap if words & set(k.split())]))[:8]
    return (
        (" Did you mean: " + ", ".join(fmt(k) for k in picks))
        if picks
        else f" Try: vocab.py search {name}"
    )


def ancestors(model: dict, section: str, key: str) -> list[str]:
    out, seen = [], set()
    while key and key not in seen:
        seen.add(key)
        parent = (model[section].get(key) or {}).get("is_a")
        if parent:
            out.append(parent)
        key = parent
    return out


def cmd_class(args) -> None:
    model = biolink()
    hit = find(model, "classes", args.name)
    if not hit:
        sys.exit(
            f"No Biolink class {args.name}." + suggest(model, "classes", args.name, curie_class)
        )
    key, c = hit
    print(f"{curie_class(key)}  ({BIOLINK_VERSION})")
    print(f"  definition: {(c.get('description') or '').strip()}")
    print(f"  is_a: {' > '.join(curie_class(a) for a in ancestors(model, 'classes', key)) or '-'}")
    if c.get("mixins"):
        print(f"  mixins: {', '.join(curie_class(m) for m in c['mixins'])}")
    if c.get("id_prefixes"):
        print(f"  id prefixes (preferred first): {', '.join(c['id_prefixes'])}")
    slots = list(c.get("slots") or [])
    for a in ancestors(model, "classes", key):
        slots += [s for s in (model["classes"].get(a, {}).get("slots") or []) if s not in slots]
    if slots:
        print(f"  slots: {', '.join(curie_slot(s) for s in slots)}")
        undefined = [s for s in slots if s not in model["slots"]]
        if undefined:
            # Biolink lists these on the class but never defines them (a Biolink quirk; see
            # vocabulary.md)
            print(
                f"  listed but not defined in Biolink {BIOLINK_VERSION}: "
                f"{', '.join(curie_slot(s) for s in undefined)}"
            )
    if c.get("slot_usage"):
        print(
            f"  slot usage: "
            f"{', '.join(f'{s}: {json.dumps(v)[:120]}' for s, v in c['slot_usage'].items())}"
        )
    if c.get("exact_mappings") or c.get("narrow_mappings"):
        print(
            f"  mappings: "
            f"{', '.join((c.get('exact_mappings') or []) + (c.get('narrow_mappings') or []))}"
        )


def cmd_slot(args) -> None:
    model = biolink()
    hit = find(model, "slots", args.name)
    if not hit:
        sys.exit(f"No Biolink slot {args.name}." + suggest(model, "slots", args.name, curie_slot))
    key, s = hit
    print(f"{curie_slot(key)}  ({BIOLINK_VERSION})")
    print(f"  definition: {(s.get('description') or '').strip()}")
    print(f"  is_a: {' > '.join(curie_slot(a) for a in ancestors(model, 'slots', key)) or '-'}")
    for prop in ("domain", "range", "multivalued", "inverse", "slot_uri"):
        if s.get(prop) is not None:
            print(f"  {prop}: {s[prop]}")
    if s.get("exact_mappings"):
        print(f"  exact mappings: {', '.join(s['exact_mappings'])}")


def _slot_range(model: dict, key: str, slot: str) -> str | None:
    """The range of slot on class key, from its own slot_usage, its mixins', or its ancestors'."""
    seen = []
    todo = [key]
    while todo:
        k = todo.pop(0)
        if k in seen or k not in model["classes"]:
            continue
        seen.append(k)
        c = model["classes"][k]
        rng = ((c.get("slot_usage") or {}).get(slot) or {}).get("range")
        if rng:
            return rng
        todo += list(c.get("mixins") or []) + ([c["is_a"]] if c.get("is_a") else [])
    return None


def cmd_associations(args) -> None:
    """Association classes whose subject/object ranges fit the given entity classes."""
    model = biolink()

    def lineage(name):
        hit = find(model, "classes", name)
        if not hit:
            sys.exit(f"No Biolink class {name}." + suggest(model, "classes", name, curie_class))
        # breadth first over is_a and mixins (and the mixins' own parents), closest first
        out, todo = [], [hit[0]]
        while todo:
            k = todo.pop(0)
            if k in out:
                continue
            out.append(k)
            c = model["classes"].get(k, {})
            todo += ([c["is_a"]] if c.get("is_a") else []) + list(c.get("mixins") or [])
        return out

    subj = lineage(args.subject) if args.subject else None
    obj = lineage(args.object) if args.object else None
    hits = []
    for k in model["classes"]:
        if "association" not in [k] + ancestors(model, "classes", k):
            continue
        s_rng, o_rng = _slot_range(model, k, "subject"), _slot_range(model, k, "object")

        def fit(rng, line, given, k=k):
            """0 for an exact range, more for a broader one; an unconstrained range fits anything
            but ranks low, unless the class name names the given class (ChemicalEntityTo... for
            ChemicalEntity)."""
            if line is None:
                return 0
            if rng is None:
                return 3 if to_key(given) in k else 12
            return line.index(rng) if rng in line else None

        fs, fo = fit(s_rng, subj, args.subject or ""), fit(o_rng, obj, args.object or "")
        if fs is None or fo is None:
            continue
        hits.append((fs + fo, k, s_rng or "(any)", o_rng or "(any)"))
    hits.sort()
    print(f"{len(hits)} association(s) whose subject/object ranges fit (closest first):")
    for _, k, s_rng, o_rng in hits[: args.limit]:
        pred = _slot_range(model, k, "predicate")
        print(
            f"  {curie_class(k)}  subject: {s_rng}; object: {o_rng}"
            + (f"; predicates: {pred}" if pred else "")
        )


MAPPING_KEYS = (
    "exact_mappings",
    "close_mappings",
    "narrow_mappings",
    "broad_mappings",
    "related_mappings",
)


def cmd_search(args) -> None:
    model = biolink()
    words = args.text.lower().replace("_", " ").split()
    for section, fmt in (("classes", curie_class), ("slots", curie_slot)):
        hits = []
        for k, v in model[section].items():
            desc = (v.get("description") or "").lower()
            names = " ".join([k] + [a.lower() for a in (v.get("aliases") or [])])
            mapped = {
                m.lower()
                for key in MAPPING_KEYS
                for m in v.get(key) or []
            }  # fmt: skip
            if args.text.lower() in mapped:  # an RO or SIO id: the Biolink term mapped to it
                score = 5
            elif " ".join(words) == k:
                score = 4
            elif all(w in names for w in words):
                score = 3
            elif all(w in names + " " + desc for w in words):
                score = 1
            else:
                continue
            hits.append((score, len(k), k, v))
        hits.sort(key=lambda h: (-h[0], h[1], h[2]))
        print(f"{section}: {len(hits)} match(es)")
        for _, _, k, v in hits[: args.limit]:
            print(f"  {fmt(k)}  — {(v.get('description') or '').strip()[:140]}")


GO_DB_XREFS_URL = (
    "https://raw.githubusercontent.com/geneontology/go-site/master/metadata/db-xrefs.yaml"
)
QUDT_UNITS_URL = "https://qudt.org/vocab/unit/"


def _refreshed(name: str, url: str, headers: dict | None = None) -> str:
    """A file that isn't versioned with Biolink, cached and refreshed after 30 days."""
    import time

    path = CACHE.parent / name
    if not path.exists() or time.time() - path.stat().st_mtime > INFORES_MAX_AGE_DAYS * 86400:
        r = requests.get(url, timeout=120, headers=headers or {})
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text)
    return path.read_text()


def go_db_xrefs() -> dict[str, dict]:
    """GO's registry of database prefixes (the DB column of GAF/GPAD/GPI, GO's db_xrefs): name ->
    entry."""
    return {
        e["database"]: e for e in yaml.safe_load(_refreshed("go-db-xrefs.yaml", GO_DB_XREFS_URL))
    }


def cmd_prefix(args) -> None:
    import bioregistry

    bl = biolink_prefixes()
    p = args.prefix
    print(f"{p}")
    if p not in bl:  # prefixes in data often differ in case from Biolink's (EMBL vs embl)
        p_bl = next((k for k in bl if k.lower() == p.lower()), None)
        if p_bl:
            print(
                f"  (Biolink spells it {p_bl}; matched ignoring case, so check it's the same "
                "namespace)"
            )
            p = p_bl
    exp = bl.get(p)
    print(f"  Biolink expansion: {exp or '(not in the Biolink prefix map)'}")
    if exp and not exp.endswith(("/", "#", "_", ":", "=")):
        print(
            f"    note: this expansion lacks a trailing delimiter; in the plan use {exp}/ or the "
            "Bioregistry URI format below (up to $1), or CURIEs expand to malformed IRIs"
        )
    delims = ("/", "#", "_", ":", "=")
    # GO's own registry of database names (GR = Gramene, AGI_LocusCode, JaponicusDB), which GO files
    # use
    try:
        go = go_db_xrefs().get(args.prefix)
    except requests.RequestException as e:
        go = None
        print(f"  GO db-xrefs: couldn't load ({e})")
    go_ns = None
    if go:
        syntax = next(
            (t.get("url_syntax") for t in go.get("entity_types") or [] if t.get("url_syntax")), None
        )
        print(f"  GO db-xrefs: {go['database']} — {go.get('name')}; url {syntax or '-'}")
        if (
            syntax
            and syntax.endswith("[example_id]")
            and syntax.split("[example_id]")[0].endswith(delims)
        ):
            go_ns = syntax.split("[example_id]")[0]
    norm = bioregistry.normalize_prefix(p) or bioregistry.normalize_prefix(p.lower())
    exact = bool(norm) and p in {
        norm,
        bioregistry.get_preferred_prefix(norm) or "",
        *(bioregistry.get_resource(norm).get_synonyms() if bioregistry.get_resource(norm) else []),
    }
    if not norm:
        guesses = [
            k
            for k, r in bioregistry.read_registry().items()
            if p.lower() in k.split(".") or p.lower() in [x.lower() for x in r.get_synonyms()]
        ]
        print(
            "  Bioregistry: no such prefix"
            + (f"; related: {', '.join(guesses[:8])}" if guesses else "")
        )
        if exp or go_ns:
            use, why = (exp, "Biolink's expansion") if exp else (go_ns, "GO's db-xrefs URL")
            print(f'  Use in the plan: "{p}": "{use}"  ({why})')
        return
    print(
        f"  Bioregistry: {norm} — {bioregistry.get_name(norm)}"
        + (
            ""
            if exact
            else f"   (matched ignoring case: {p} may be a different namespace; check the name)"
        )
    )
    print(f"    preferred prefix: {bioregistry.get_preferred_prefix(norm) or norm}")
    print(f"    local id pattern: {bioregistry.get_pattern(norm)}")
    print(f"    example: {bioregistry.get_example(norm)}")
    uri_format = bioregistry.get_uri_format(norm)
    print(f"    URI format: {uri_format}")
    # what to put in the plan's prefixes: Biolink's expansion if it's a clean namespace, else GO's
    # own URL when the
    # Bioregistry match is only by case, else Bioregistry's URI format up to $1 if that ends in a
    # delimiter, else
    # Bioregistry's resolver
    from_bioregistry = (
        uri_format.split("$1")[0] if uri_format and uri_format.endswith("$1") else None
    )
    # Biolink's map has some expansions that aren't namespaces: query URLs, or a resolver URL with
    # part of an id
    # baked in (kegg.genes -> https://bioregistry.io/kegg.genes:bsu:, one organism)
    usable = (
        bool(exp)
        and exp.endswith(delims)
        and "?" not in exp
        and not ("bioregistry.io/" in exp and exp.split("bioregistry.io/", 1)[1].count(":") > 1)
    )
    if usable:
        use, why = exp, "Biolink's expansion"
    elif go_ns and not exact:
        use, why = go_ns, f"GO's db-xrefs URL (Bioregistry's {norm} matched only ignoring case)"
    elif from_bioregistry and from_bioregistry.endswith(delims):
        use, why = (
            from_bioregistry,
            (
                "Bioregistry's URI format (Biolink's expansion isn't a plain namespace)"
                if exp
                else "Bioregistry's URI format (Biolink doesn't have this prefix)"
            ),
        )
    elif go_ns:
        use, why = go_ns, "GO's db-xrefs URL"
    else:
        use, why = (
            f"https://bioregistry.io/{norm}:",
            (
                "Bioregistry's resolver (no usable namespace URL; the knowledge "
                "base's or database's own record URL is fine too, if it ends in "
                "a delimiter)"
            ),
        )
    print(f'  Use in the plan: "{p}": "{use}"  ({why})')
    preferred = bioregistry.get_preferred_prefix(norm) or norm
    if preferred != p:
        print(
            f"    spelling: declare the prefix as the data writes it; Bioregistry prefers "
            f"{preferred}" + (f", Biolink uses {p}" if exp else "")
        )
    model = biolink()
    users = [
        curie_class(k) for k, v in model["classes"].items() if p in (v.get("id_prefixes") or [])
    ]
    if users:
        print(f"  Biolink classes that list {p} as an id prefix: {', '.join(users[:12])}")


def qudt_units() -> list[dict]:
    """QUDT units: [{id: unit:X, label, ucum, symbol}], from QUDT's unit vocabulary (cached)."""
    ttl = _refreshed("qudt-units.ttl", QUDT_UNITS_URL, {"Accept": "text/turtle"})
    units = []
    for block in re.split(r"\n(?=unit:\S+\s*\n)", ttl):
        head = re.match(r"(unit:\S+)\s*\n", block)
        if not head or "a qudt:Unit" not in block and "qudt:Unit" not in block.split(";")[0]:
            continue
        labels = (
            re.findall(r'rdfs:label "([^"]+)"@en\b', block)
            or re.findall(r'rdfs:label "([^"]+)"(?!@)', block)
            or re.findall(r'rdfs:label "([^"]+)"', block)
        )
        ucum = re.findall(r'qudt:ucumCode "([^"]+)"', block)
        symbol = re.findall(r'qudt:symbol "([^"]+)"', block)
        units.append(
            {
                "id": head.group(1),
                "label": labels[0] if labels else "",
                "ucum": ucum,
                "symbol": symbol,
            }
        )
    return units


def ucum_division(code: str) -> str:
    """QUDT writes UCUM codes with negative exponents: 'kg/m2' -> 'kg.m-2', 'mg/dL' -> 'mg.dL-1'."""
    if " " in code.strip() or code.count("/") != 1:
        return code
    top, bottom = code.split("/")
    parts = []
    for u in bottom.split("."):
        m = re.fullmatch(r"(.*?[^\d-])(\d*)", u)
        parts.append(f"{m.group(1)}-{m.group(2) or 1}" if m else u)
    return ".".join([top, *parts]) if top not in ("", "1") else ".".join(parts)


def cmd_unit(args) -> None:
    args.text = ucum_division(args.text)
    words = []
    for w in args.text.lower().split():
        # "nanomolar" is "nanomole per litre" in QUDT
        words += [w[:-5] + "mol", "per", "lit"] if w.endswith("molar") else [w]
    hits = []
    for u in qudt_units():
        hay = " ".join(
            [
                u["id"].lower(),
                u["label"].lower(),
                *map(str.lower, u["ucum"]),
                *map(str.lower, u["symbol"]),
            ]
        )
        if all(w in hay for w in words):
            exact = args.text.lower() in [
                u["label"].lower(),
                *map(str.lower, u["ucum"]),
                u["id"].lower()[5:],
            ]
            hits.append((not exact, len(u["label"]), u))
    if not hits:
        sys.exit(
            f'No QUDT unit matches {args.text!r}. Try other words ("per second", "micromolar"), a '
            "UCUM "
            "code (umol/L), or a UO term with vocab.py ols TEXT --ontology uo"
        )
    for _, _, u in sorted(hits, key=lambda h: h[:2])[: args.limit]:
        print(
            f"  {u['id']:28} {u['label']}"
            + (f"   ucum {', '.join(u['ucum'][:3])}" if u["ucum"] else "")
        )


def _literal_letters(pattern: str) -> int:
    """How many letters a pattern pins down outside character classes and escapes."""
    stripped = re.sub(r"\\[a-zA-Z]|\[[^\]]*\]|\{[^}]*\}", "", pattern)
    return len(re.findall(r"[A-Za-z]", stripped))


CATCH_ALL_PROBES = ["zq9", "Xyzzy12", "QWERT9876", "ab_12c", "K7"]


def _anchor(pattern: str, values: list[str]) -> int:
    """Length of the pattern's literal letter runs that the values start with (DTXSID for
    ^DTXSID\\d+$). Catch-all patterns score 0, so patterns that name the values' own letters rank
    first."""
    runs = re.findall(r"[A-Za-z]{2,}", re.sub(r"\\[a-zA-Z]|\[[^\]]*\]|\{[^}]*\}", " ", pattern))
    return max((len(r) for r in runs if all(v.startswith(r) for v in values)), default=0)


def cmd_identify(args) -> None:
    shapes: dict[str, list[str]] = {}
    for v in args.values:
        # letters kept (DTXSID9 vs ENSG9) except single letters, which vary within one scheme (MeSH
        # C/D)
        shapes.setdefault(re.sub(r"\b[A-Za-z](?=\d)", "A", re.sub(r"\d+", "9", v)), []).append(v)
    if len(shapes) > 1:
        print(
            f"The values have {len(shapes)} different shapes, so they probably use different "
            "prefixes; "
            "checking each shape separately.\n"
        )
        for vals in shapes.values():
            print(f"== {', '.join(vals[:4])}")
            _identify(vals, args.limit)
            print()
        return
    _identify(args.values, args.limit)


def _identify(values: list[str], limit: int) -> None:
    import bioregistry

    bl = {k.lower(): k for k in biolink_prefixes()}
    if all(v.isdigit() for v in values):
        print(
            "All values are plain integers. Many registries use numeric ids, so the prefix must "
            "come from "
            "the column name or the knowledge base's documentation, not the values."
        )
        return
    hits = []
    for prefix, resource in bioregistry.read_registry().items():
        pattern = resource.get_pattern()
        if not pattern:
            continue
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        if sum(bool(rx.fullmatch(p)) for p in CATCH_ALL_PROBES) >= 2:
            # patterns that match arbitrary strings (^\\w+$, Ensembl's fallback) say nothing
            continue
        if all(rx.fullmatch(v) for v in values):
            names = {
                prefix,
                bioregistry.get_preferred_prefix(prefix) or prefix,
                *resource.get_synonyms(),
            }
            in_bl = next((bl[n.lower()] for n in names if n.lower() in bl), None)
            hits.append(
                (
                    -_anchor(pattern, values),
                    in_bl is None,
                    len(pattern),
                    prefix,
                    in_bl,
                    resource.get_name(),
                    pattern,
                )
            )
    hits.sort()
    hits = [h[1:] for h in hits]
    if not hits:
        print(
            "No registered prefix has a specific pattern that fits; the prefix must come from "
            "documentation."
        )
        return
    print(
        f"{len(hits)} registered prefix(es) whose id pattern pins down these values (most specific "
        "first):"
    )
    for _, _, prefix, in_bl, name, pattern in hits[:limit]:
        print(
            f"  {(in_bl or prefix):16} {name}   pattern {pattern}"
            + ("" if in_bl else "   [not a Biolink prefix]")
        )


def cmd_term(args) -> None:
    """Look up an ontology term by its CURIE (exact; free-text search rarely finds exact labels).
    Classes, then properties (IAO:0000115 definition, RO relations), then individuals."""
    terms, kind = [], "term"
    for kind in ("terms", "properties", "individuals"):
        r = requests.get(
            f"https://www.ebi.ac.uk/ols4/api/{kind}", params={"obo_id": args.curie}, timeout=60
        )
        r.raise_for_status()
        terms = (r.json().get("_embedded") or {}).get(kind) or []
        if terms:
            break
    if not terms:
        sys.exit(f"OLS has no class, property or individual {args.curie}")
    t = next((t for t in terms if t.get("is_defining_ontology")), terms[0])
    what = {"terms": "", "properties": " [property]", "individuals": " [individual]"}[kind]
    print(f"{args.curie}  {t.get('label')}  ({t.get('ontology_name')}){what}")
    # OLS lists the definition alongside comments in "description", in no fixed order: show them all
    for text_ in t.get("description") or ["(no definition)"]:
        print(f"  description: {text_}")
    if t.get("is_obsolete"):
        print("  OBSOLETE")


def cmd_ols(args) -> None:
    params = {"q": args.text, "rows": args.limit}
    if args.ontology:
        params["ontology"] = args.ontology
    r = requests.get("https://www.ebi.ac.uk/ols4/api/search", params=params, timeout=60)
    r.raise_for_status()
    for d in r.json()["response"]["docs"]:
        desc = (d.get("description") or [""])[0]
        print(
            f"  {d.get('obo_id') or d.get('iri')}  {d.get('label')}  ({d.get('ontology_name')})  "
            f"{desc[:100]}"
        )


def cmd_infores(args) -> None:
    words = args.text.lower().split()
    hits = []
    for iid, e in infores().items():
        hay = " ".join(
            [iid, e.get("name") or "", *(e.get("synonym") or []), *(e.get("xref") or [])]
        ).lower()
        if all(w in hay for w in words):
            exact = args.text.lower() in (
                [iid.split(":")[1], (e.get("name") or "").lower()]
                + [x.lower() for x in e.get("synonym") or []]
            )
            hits.append((not exact, e.get("status") != "released", iid, e))
    hits.sort(key=lambda h: h[:3])
    if not hits:
        sys.exit(
            f"No infores entry matches {args.text!r}. If the source has none, leave it unmapped "
            "and list it "
            "in notes.md (a new infores entry is the fix)."
        )
    for _, _, iid, e in hits[: args.limit]:
        print(
            f"  {iid}  {e.get('name')}  [{e.get('status')}; {e.get('knowledge_level', '-')}]  "
            f"{', '.join(e.get('xref') or [])[:80]}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(required=True)
    for name, fn, arg in (
        ("class", cmd_class, "name"),
        ("slot", cmd_slot, "name"),
        ("prefix", cmd_prefix, "prefix"),
    ):
        p = sub.add_parser(name)
        p.add_argument(arg)
        p.set_defaults(fn=fn)
    p = sub.add_parser("associations", help="association classes for a subject and/or object class")
    p.add_argument("--subject")
    p.add_argument("--object")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(fn=cmd_associations)
    p = sub.add_parser("search")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("identify")
    p.add_argument("values", nargs="+")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_identify)
    p = sub.add_parser("term", help="an ontology term by CURIE, e.g. ECO:0000315")
    p.add_argument("curie")
    p.set_defaults(fn=cmd_term)
    p = sub.add_parser("infores", help="information-resource ids for a source name")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_infores)
    p = sub.add_parser(
        "unit", help="QUDT units by label, UCUM code or symbol (for a column's unit)"
    )
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(fn=cmd_unit)
    p = sub.add_parser("ols")
    p.add_argument("text")
    p.add_argument("--ontology")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(fn=cmd_ols)
    args = ap.parse_args()
    try:
        args.fn(args)
    except (
        requests.RequestException
    ) as e:  # OLS, Bioregistry, QUDT or GitHub unreachable: say so in one line
        sys.exit(
            f"{args.fn.__name__.removeprefix('cmd_')}: couldn't reach "
            f"{getattr(e.request, 'url', 'the service')}: "
            f"{type(e).__name__}. Try again; if it persists, the service may be down."
        )


if __name__ == "__main__":
    main()
