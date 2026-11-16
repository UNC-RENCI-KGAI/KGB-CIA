"""The code quote repair (quote_align.py) on short made-up abstracts, one test per way the label
model misquotes (the failure kinds measured on 2026-11-16): every quote it returns is found by
`quotes.quote_found`, and a quote that isn't in the abstract gets none."""

from __future__ import annotations

import pytest
from pubmed_evidence import quote_align
from pubmed_evidence.quotes import quote_found

TITLE = "Imatinib for chronic myeloid leukemia: a randomized trial"
S1 = "We randomized 120 patients with chronic myeloid leukemia to imatinib or interferon."
S2 = "Patients were followed for a median of 19 months."
S3 = (
    "Thus, imatinib induced complete cytogenetic responses in 76% of patients, against 15% with "
    "interferon."
)
S4 = "Adverse events, e.g. nausea and edema, were mild in most patients."
S5 = "Imatinib was well tolerated and superior to interferon-alpha in newly diagnosed patients."
ABSTRACT = " ".join([S1, S2, S3, S4, S5])

IMATINIB = ["imatinib", "Gleevec", "STI571"]
CML = ["chronic myeloid leukemia", "CML"]


def repaired(quote: str, entities=(), claim: str = "") -> tuple[str | None, dict]:
    """The repair of a quote the check rejects, checked as repair.py checks it."""
    assert not quote_found(quote, TITLE, ABSTRACT), "the misquote must fail the check"
    fixed, info = quote_align.repair(quote, TITLE, ABSTRACT, entities, claim)
    if fixed is not None:
        assert quote_found(fixed, TITLE, ABSTRACT)
    return fixed, info


def test_stitched_sentences_are_quoted_whole_and_joined_with_an_ellipsis():
    # S1 and S3 run together, S2 skipped, with no '...'.
    quote = f"{S1} Thus, imatinib induced complete cytogenetic responses in 76% of patients"
    assert repaired(quote)[0] == f"{S1} ... {S3}"


def test_a_small_paraphrase_finds_its_sentence():
    quote = S3.replace("Thus", "Therefore").replace("76%", "seventy-six percent")
    assert repaired(quote)[0] == S3


def test_punctuation_and_hyphens():
    quote = (
        "Imatinib was well-tolerated and superior to interferon alpha in newly-diagnosed patients"
    )
    assert repaired(quote)[0] == S5


def test_a_title_prefix_finds_the_title():
    fixed, info = repaired(f"Title: {TITLE}")
    assert fixed == TITLE and info["coverage"] >= quote_align.FLOOR


def test_words_dropped_inside_a_sentence():
    # 'e.g.' doesn't end a sentence, so S4 is quoted whole.
    assert repaired("Adverse events were mild in most patients")[0] == S4


def test_an_ellipsis_misused_out_of_order_or_too_short():
    out_of_order = (
        "imatinib induced complete cytogenetic responses in 76% of patients ... "
        "We randomized 120 patients with chronic myeloid leukemia"
    )
    assert repaired(out_of_order)[0] == f"{S1} ... {S3}"
    # '19 months' is too short a part, and adds too little to bring S2 in.
    too_short = "Thus, imatinib induced complete cytogenetic responses ... 19 months"
    assert repaired(too_short)[0] == S3


def test_title_and_abstract_mixed_are_quoted_from_one_of_them():
    mixed = (
        "Imatinib for chronic myeloid leukemia ... imatinib induced complete cytogenetic "
        "responses in 76% of patients"
    )
    # The abstract holds more of it than the title does.
    fixed, _ = repaired(mixed)
    assert fixed == S3 and quote_found(fixed, "", ABSTRACT)
    # The disease is joined from the abstract, never from the title.
    fixed, info = repaired(mixed, [IMATINIB, CML])
    assert fixed == f"{S1} ... {S3}" and info["joined"]


def test_a_quote_not_in_the_abstract_gets_none():
    quote = "Dasatinib eliminated leukemic stem cells in every mouse we treated"
    fixed, info = quote_align.repair(quote, TITLE, ABSTRACT, [IMATINIB, CML])
    assert fixed is None and info["coverage"] < quote_align.FLOOR
    assert quote_align.repair("", TITLE, ABSTRACT) == (None, {"coverage": 0.0})
    assert quote_align.repair(quote, "", "") == (None, {"coverage": 0.0})


def test_a_sentence_naming_the_missing_entity_is_joined():
    quote = "Therefore, imatinib induced complete cytogenetic responses in 76% of patients"
    fixed, info = repaired(quote, [IMATINIB, CML], "imatinib treats chronic myeloid leukemia")
    assert fixed == f"{S1} ... {S3}" and info["joined"]
    # Named both already: nothing to join.
    assert repaired(f"{S1[:-1]}!", [IMATINIB, CML])[0] == S1


def test_unresolved_when_no_sentence_names_the_entity():
    quote = "Therefore, imatinib induced complete cytogenetic responses in 76% of patients"
    fixed, info = repaired(quote, [IMATINIB, ["dasatinib"]])
    assert fixed == S3 and info["unresolved"] and "joined" not in info


def test_entities_are_named_by_synonym_stem_or_defined_abbreviation():
    context = "Patients with rheumatoid arthritis (RA) were enrolled. RA activity fell."
    assert quote_align.mentions("RA activity fell.", ["rheumatoid arthritis"], context)
    assert not quote_align.mentions("RA activity fell.", ["rheumatoid arthritis"])
    assert quote_align.mentions("Gleevec was given daily.", IMATINIB)
    # A synonym's generic words are optional, and words match on their first six letters.
    assert quote_align.mentions("in hypertensive adults", ["hypertension disease"])
    assert not quote_align.mentions("in diabetic adults", ["hypertension disease"])


def test_structured_abstract_labels_and_lists_split_as_sentences():
    abstract = (
        "BACKGROUND: Two aims were set. 1. To measure the response rate.\n"
        "RESULTS: Imatinib induced responses in J. Smith's cohort of 40 patients."
    )
    sents = quote_align.split_sentences("A title.", abstract)
    assert [(s.source, s.para, s.text) for s in sents] == [
        ("title", 0, "A title."),
        ("abstract", 0, "Two aims were set."),
        ("abstract", 0, "1. To measure the response rate."),
        ("abstract", 1, "Imatinib induced responses in J. Smith's cohort of 40 patients."),
    ]
    assert all(abstract[s.start : s.end] == s.text for s in sents if s.source == "abstract")
    # A quote that kept the section label gets the sentence without it.
    quote = "RESULTS: Imatinib induced responses in J. Smith's cohort of forty patients"
    fixed, _ = quote_align.repair(quote, "A title.", abstract)
    assert fixed == "Imatinib induced responses in J. Smith's cohort of 40 patients."


@pytest.mark.parametrize(
    "quote",
    [
        f"{S2} {S3}",  # adjacent sentences: one run, no ellipsis
        "patients were followed ... imatinib induced complete cytogenetic responses",
        S5.upper(),
    ],
)
def test_every_quote_returned_is_verbatim(quote):
    fixed, _ = quote_align.repair(quote, TITLE, ABSTRACT, [IMATINIB, CML])
    assert fixed is not None and quote_found(fixed, TITLE, ABSTRACT)
    assert all(part in ABSTRACT or part in TITLE for part in fixed.split(" ... "))
