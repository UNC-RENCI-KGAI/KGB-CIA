"""Step 4 (ground) without the network: the Name Resolver and NodeNorm as fake functions or
`httpx.MockTransport`, the toy UBERON release from `toy.py`, and the work files and the label
cache's claims written in code."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest
from pubmed_evidence import config, ground, records, refine, uberon
from pubmed_evidence.cache import AbstractClaimsRow, LabelCache, PubMedCache
from pubmed_evidence.graphio import write_jsonl
from pubmed_evidence.records import PUBMED, LabelRun
from toy import UBERON_OBO, UBERON_VERSION

from shared import nodenorm
from shared.pubmed import PubmedRecord

SYSTEM, ORGAN, DIGESTIVE, RENAL = (
    "UBERON:0000467",
    "UBERON:0000062",
    "UBERON:0001007",
    "UBERON:0001008",
)
LIVER, PARENCHYMA, PRIMORDIUM, KIDNEY = (
    "UBERON:0002107",
    "UBERON:0001280",
    "UBERON:0003894",
    "UBERON:0002113",
)
LYMPH_NODE, SKIN, PLASMA = "UBERON:0000029", "UBERON:0002097", "UBERON:0001969"

# --- UBERON's closure -------------------------------------------------------------------------


def test_parse_builds_the_is_a_and_part_of_closure():
    release = uberon.parse(UBERON_OBO.splitlines())
    assert release.version == UBERON_VERSION
    # is_a and part_of, in any mix; develops_from isn't a parent.
    assert release.ancestors[PARENCHYMA] == {LIVER, ORGAN, DIGESTIVE, SYSTEM}
    assert release.ancestors[LIVER] == {ORGAN, DIGESTIVE, SYSTEM}
    assert release.ancestors[KIDNEY] == {ORGAN, RENAL, SYSTEM}
    assert release.ancestors[PRIMORDIUM] == release.ancestors[SYSTEM] == frozenset()
    # Live terms only; a relation's stanza isn't a term.
    assert set(release.ancestors) == {
        SYSTEM,
        ORGAN,
        DIGESTIVE,
        RENAL,
        LIVER,
        PARENCHYMA,
        PRIMORDIUM,
        KIDNEY,
    }


def test_parse_survives_a_cycle_and_a_missing_version():
    obo = "[Term]\nid: X:1\nis_a: X:2\n\n[Term]\nid: X:2\nrelationship: part_of X:1 ! one\n"
    release = uberon.parse(obo.splitlines())
    assert release.version is None
    assert release.ancestors == {"X:1": {"X:2"}, "X:2": {"X:1"}}


def test_closure_decides_which_anatomy_is_at_or_below_which():
    lattice = refine.Lattice(anatomy=uberon.parse(UBERON_OBO.splitlines()).ancestors)
    anatomy = refine.ANATOMY
    assert lattice.at_or_below(anatomy, LIVER, LIVER)
    assert lattice.at_or_below(anatomy, LIVER, DIGESTIVE)
    assert lattice.at_or_below(anatomy, PARENCHYMA, SYSTEM)  # part_of twice, then is_a
    assert not lattice.at_or_below(anatomy, DIGESTIVE, LIVER)
    assert not lattice.at_or_below(anatomy, LIVER, KIDNEY)
    assert not lattice.at_or_below(anatomy, LIVER, PRIMORDIUM)  # develops_from isn't a parent


def fake_obolibrary(served: list[str], body: httpx.Response | None = None):
    """The release's PURL redirects to GitHub, which serves the file (or `body`)."""

    def handle(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        if request.url.host == "purl.obolibrary.org":
            return httpx.Response(302, headers={"Location": "https://github.com/uberon.obo"})
        return body or httpx.Response(200, text=UBERON_OBO)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_ensure_fetches_the_pinned_release_once(tmp_path):
    served: list[str] = []
    path = tmp_path / "cache" / config.UBERON_FILE
    release = uberon.ensure(path, client=fake_obolibrary(served))
    assert served == [config.UBERON_URL, "https://github.com/uberon.obo"]
    assert path.read_text() == UBERON_OBO and release.version == UBERON_VERSION
    # It's kept: a second load reads the file.
    assert uberon.ensure(path, client=fake_obolibrary(served)) == release
    assert len(served) == 2


@pytest.mark.parametrize(
    "body", [httpx.Response(503), httpx.Response(200, text="<html>Too many requests</html>")]
)
def test_a_failed_fetch_leaves_nothing_behind(tmp_path, body):
    path = tmp_path / config.UBERON_FILE
    with pytest.raises((httpx.HTTPError, ValueError)):
        uberon.fetch(path, client=fake_obolibrary([], body))
    assert list(tmp_path.iterdir()) == []


# --- Grounding names --------------------------------------------------------------------------


def match(curie: str, label: str, *synonyms: str) -> dict:
    return {"curie": curie, "label": label, "synonyms": list(synonyms), "types": [], "score": 1}


def entry(curie: str, label: str, *equivalents: tuple[str, str]) -> dict:
    idents = [{"identifier": c, "label": n} for c, n in [(curie, label), *equivalents]]
    return {"id": idents[0], "equivalent_identifiers": idents, "type": []}


# The Name Resolver's matches, best first, as it ranks them; and NodeNorm's cliques.
MATCHES = {
    "liver": [match(LIVER, "liver", "hepar"), match("UMLS:C4037986", "Abdomen>Liver", "Liver")],
    "kidneys": [match(KIDNEY, "kidney", "kidneys", "renal organ")],
    # A near match isn't the name.
    "lymph nodes": [
        match("UMLS:C0935035", "Set of visceral abdominal lymph nodes"),
        match(LYMPH_NODE, "lymph node", "lymph nodes", "lymph gland"),
    ],
    # The first exact match is no UBERON term; the second is.
    "skin": [match("UMLS:C1123023", "Skin", "skin"), match(SKIN, "skin of body", "Skin")],
    # A cell type is no anatomy context, and nothing names a tumour margin.
    "hepatocytes": [match("CL:0000182", "hepatocyte", "hepatocytes")],
    "tumour margin": [match("UMLS:C1184544", "Gingival margin", "gum margin")],
    # A clique whose preferred id isn't UBERON's but has an UBERON term.
    "plasma": [match("MESH:D010949", "Plasma", "plasma")],
}
NODENORM = {
    LIVER: entry(LIVER, "liver"),
    "UMLS:C4037986": entry("UMLS:C4037986", "Abdomen>Liver"),
    KIDNEY: entry(KIDNEY, "kidney"),
    LYMPH_NODE: entry(LYMPH_NODE, "lymph node"),
    "UMLS:C1123023": entry("UMLS:C1123023", "Skin"),
    SKIN: entry(SKIN, "skin of body"),
    "CL:0000182": entry("CL:0000182", "hepatocyte"),
    "MESH:D010949": entry("MESH:D010949", "Plasma", (PLASMA, "blood plasma")),
}


class Fakes:
    """The Name Resolver and NodeNorm, keeping what each was asked."""

    def __init__(self):
        self.looked_up: list[list[str]] = []
        self.normalized: list[list[str]] = []

    def lookup(self, names: list[str]) -> dict[str, list[dict]]:
        self.looked_up.append(list(names))
        return {n: MATCHES.get(n, []) for n in names}

    def nodenorm(self, curies: list[str], **kw) -> dict:
        self.normalized.append(list(curies))
        return {c: NODENORM.get(c) for c in curies}


def test_anatomy_key():
    assert ground.anatomy_key("  Lymph\tNodes ") == "lymph nodes"
    assert ground.claim_anatomy({"context": {"anatomy": "Liver"}}) == "Liver"
    for claim in ({}, {"context": None}, {"context": {"anatomy": " "}}):
        assert ground.claim_anatomy(claim) is None


def test_names_ground_to_the_first_exact_match_with_an_uberon_term(tmp_path):
    fakes = Fakes()
    names = ["Liver", " liver ", "kidneys", "lymph nodes", "skin", "hepatocytes", "tumour margin"]
    with PubMedCache() as cache:
        s = ground.ground_anatomy(
            [*names, "plasma", ""], cache, lookup=fakes.lookup, nodenorm=fakes.nodenorm
        )
        keys = ["liver", "kidneys", "lymph nodes", "skin", "hepatocytes", "tumour margin", "plasma"]
        assert fakes.looked_up == [keys]
        # Only exact matches go to NodeNorm: not the set of lymph nodes, nor the gingival margin.
        assert set(fakes.normalized[0]) == set(NODENORM)
        got = ground.groundings(cache, [*names, "Plasma", "spleen"])
        assert {n: g.curie for n, g in got.items()} == {
            "Liver": LIVER,
            " liver ": LIVER,
            "kidneys": KIDNEY,
            "lymph nodes": LYMPH_NODE,
            "skin": SKIN,
            "hepatocytes": None,
            "tumour margin": None,
            "Plasma": PLASMA,
        }
        liver, plasma, missed = got["Liver"], got["Plasma"], got["hepatocytes"]
        assert (liver.text, liver.kind, liver.label, liver.source) == (
            "liver",
            "anatomy",
            "liver",
            ground.SOURCE,
        )
        assert plasma.label == "blood plasma" and missed.label is None
    assert s == {
        "names": 7,
        "cached": 0,
        "looked_up": 7,
        "grounded": 5,
        "ungrounded": 2,
        "failed": 0,
        "failed_names": [],
        "terms": sorted([LIVER, KIDNEY, LYMPH_NODE, SKIN, PLASMA]),
    }


def test_cached_names_are_not_asked_again_unless_refreshed(tmp_path):
    fakes = Fakes()
    with PubMedCache() as cache:
        ground.ground_anatomy(
            ["liver", "hepatocytes"], cache, lookup=fakes.lookup, nodenorm=fakes.nodenorm
        )
        s = ground.ground_anatomy(
            ["LIVER", "hepatocytes", "kidneys"], cache, lookup=fakes.lookup, nodenorm=fakes.nodenorm
        )
        assert fakes.looked_up == [["liver", "hepatocytes"], ["kidneys"]]
        assert (s["cached"], s["looked_up"], s["grounded"], s["ungrounded"]) == (2, 1, 2, 1)
        # NodeNorm's entries are cached too, in step 2's table.
        assert cache.get_nodenorm([LIVER])[LIVER] == NODENORM[LIVER]
        s = ground.ground_anatomy(
            ["liver"], cache, refresh=True, lookup=fakes.lookup, nodenorm=fakes.nodenorm
        )
        assert fakes.looked_up[-1] == ["liver"] and s["looked_up"] == 1
        assert len(fakes.normalized) == 2


def test_failed_lookups_are_not_cached(tmp_path, monkeypatch, capsys):
    fakes = Fakes()
    monkeypatch.setattr(ground, "NAMERES_BATCH", 2)

    def flaky(names):
        if "kidneys" in names:
            raise httpx.ConnectError("refused")
        return fakes.lookup(names)

    def nodenorm_down(curies):
        raise httpx.ReadTimeout("slow")

    names = ["liver", "skin", "kidneys"]
    with PubMedCache() as cache:
        s = ground.ground_anatomy(names, cache, lookup=flaky, nodenorm=fakes.nodenorm)
        assert (s["failed"], s["failed_names"], s["grounded"]) == (1, ["kidneys"], 2)
        assert "lookup of 1 names failed (ConnectError: refused)" in capsys.readouterr().err
        assert "kidneys" not in ground.groundings(cache, names)
        # NodeNorm failing fails the names whose matches it was to normalize.
        s = ground.ground_anatomy(names, cache, lookup=fakes.lookup, nodenorm=nodenorm_down)
        assert (s["cached"], s["failed_names"]) == (2, ["kidneys"])
        # The rerun grounds them.
        s = ground.ground_anatomy(names, cache, lookup=fakes.lookup, nodenorm=fakes.nodenorm)
        assert (s["failed"], s["grounded"]) == (0, 3)


def test_name_lookup_request():
    sent: list[tuple[str, dict]] = []
    reply: list[httpx.Response] = [httpx.Response(200, json={"liver": MATCHES["liver"]})]

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), json.loads(request.content)))
        return reply[0]

    client = httpx.Client(transport=httpx.MockTransport(handle))
    url = "https://nameres.example.org/"
    assert ground.name_lookup(["liver"], client=client, url=url) == {"liver": MATCHES["liver"]}
    assert sent == [
        (
            "https://nameres.example.org/bulk-lookup",
            {
                "strings": ["liver"],
                "autocomplete": False,
                "limit": ground.NAMERES_HITS,
                "biolink_types": ["biolink:AnatomicalEntity"],
            },
        )
    ]
    assert ground.name_lookup([], client=client) == {} and len(sent) == 1
    reply[0] = httpx.Response(200, json=["liver"])
    with pytest.raises(ValueError):
        ground.name_lookup(["liver"], client=client)
    reply[0] = httpx.Response(502)
    with pytest.raises(httpx.HTTPStatusError):
        ground.name_lookup(["liver"], client=client)


# --- The step ---------------------------------------------------------------------------------

MODEL, VERSION = "gpt-6-luna", "label@1"


def claim(anatomy: str | None) -> dict:
    """A claim as `abstract_claims` stores it; the step reads only its context."""
    return {
        "polarity": "positive",
        "statement": {"subject": "CHEBI:1", "predicate": "biolink:affects", "object": "NCBIGene:2"},
        "context": {"species": "mouse", "anatomy": anatomy, "notes": None},
        "evidence_sentence": "X raised Y.",
    }


def setup_work(tmp_path: Path) -> argparse.Namespace:
    """Two searched pairs whose claims name the liver (twice) and the kidneys, and claims the
    step mustn't read: another run's, and those of a pair this graph doesn't have."""
    work, cache_dir = tmp_path / "work", tmp_path / "cache"
    write_jsonl(
        work / PUBMED,
        [PubmedRecord(pair_id=p, query="q", retrieved="2026-11-05", total=1) for p in ("p1", "p2")],
    )
    records.write_label_run(work, LabelRun(model=MODEL, prompt_version=VERSION))

    def row(pair: str, pmid: str, claims: list[dict], version: str = VERSION):
        return AbstractClaimsRow(pair, pmid, version, MODEL, "animal", claims)

    with LabelCache(cache_dir / config.LABEL_CACHE) as labels:
        labels.put_abstract_claims(
            [
                row("p1", "1", [claim("Liver"), claim(None)]),
                row("p1", "2", []),
                row("p2", "3", [claim("liver"), claim("kidneys")]),
                row("p2", "3", [claim("skin")], version="label@0"),
                row("p9", "4", [claim("hepatocytes")]),
            ]
        )
    parser = argparse.ArgumentParser()
    ground.add_arguments(parser)
    args = parser.parse_args([])
    args.work, args.cache_dir = work, cache_dir
    args.input, args.output = tmp_path / "filter", tmp_path / "out"
    return args


@pytest.fixture
def fakes(monkeypatch) -> tuple[Fakes, list[Path]]:
    """The Name Resolver, NodeNorm and the UBERON download, as the step reaches them."""
    fakes, fetched = Fakes(), []

    def fetch(path: Path, url: str = config.UBERON_URL, client=None) -> None:
        fetched.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UBERON_OBO)

    monkeypatch.setattr(ground, "name_lookup", fakes.lookup)
    monkeypatch.setattr(nodenorm, "normalize", fakes.nodenorm)
    monkeypatch.setattr(uberon, "fetch", fetch)
    return fakes, fetched


def test_run_grounds_the_runs_claims_and_pins_uberon(tmp_path, fakes, capsys):
    names, fetched = fakes
    args = setup_work(tmp_path)
    assert not args.refresh
    assert ground.run(args) == 0
    # This run's claims of this graph's pairs only.
    assert names.looked_up == [["liver", "kidneys"]]
    assert fetched == [args.cache_dir / config.UBERON_FILE]
    run = records.read_label_run(args.work)
    assert run == LabelRun(model=MODEL, prompt_version=VERSION, uberon_version=UBERON_VERSION)
    out = capsys.readouterr().out
    assert "ground: 2 anatomy names in 4 claims of run gpt-6-luna label@1: 2 grounded" in out
    assert f"ground: UBERON {UBERON_VERSION}, 8 terms" in out

    # A rerun asks nothing and fetches nothing.
    assert ground.run(args) == 0
    assert len(names.looked_up) == 1 and len(fetched) == 1
    assert "2 from cache, 0 looked up, 0 failed" in capsys.readouterr().out


def test_run_warns_of_terms_the_release_lacks(tmp_path, fakes, capsys, monkeypatch):
    monkeypatch.setitem(MATCHES, "kidneys", [match(LYMPH_NODE, "lymph node", "kidneys")])
    assert ground.run(setup_work(tmp_path)) == 0
    assert "1 grounded terms aren't live in this UBERON release (e.g. UBERON:0000029)" in (
        capsys.readouterr().err
    )


def test_run_needs_the_pubmed_step_and_a_label_run(tmp_path, fakes, capsys):
    args = setup_work(tmp_path)
    (args.work / records.LABEL_RUN).unlink()
    assert ground.run(args) == 1
    assert "run `kgb-augment label` first" in capsys.readouterr().err
    (args.work / PUBMED).unlink()
    assert ground.run(args) == 1
    assert "run `kgb-augment pubmed` first" in capsys.readouterr().err
    assert fakes[0].looked_up == []


def test_run_fails_when_uberon_cant_be_fetched(tmp_path, fakes, monkeypatch, capsys):
    def down(path: Path, url: str = config.UBERON_URL, client=None) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(uberon, "fetch", down)
    args = setup_work(tmp_path)
    assert ground.run(args) == 1
    assert "couldn't fetch UBERON" in capsys.readouterr().err
    # The names are grounded and cached all the same; the run names no release.
    assert records.read_label_run(args.work).uberon_version is None
    with PubMedCache() as cache:
        assert set(ground.groundings(cache, ["liver", "kidneys"])) == {"liver", "kidneys"}


def test_run_fails_when_many_lookups_fail(tmp_path, fakes, monkeypatch, capsys):
    def down(names):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ground, "name_lookup", down)
    args = setup_work(tmp_path)
    assert ground.run(args) == 1
    assert "2 names failed and aren't cached; rerun to retry" in capsys.readouterr().err
    # A small share of failures is tolerated, as in step 2.
    monkeypatch.setattr(ground, "MAX_FAILED_SHARE", 1.0)
    assert ground.run(args) == 0
