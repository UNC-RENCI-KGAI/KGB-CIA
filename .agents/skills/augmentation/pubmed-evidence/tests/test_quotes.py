from pubmed_evidence.quotes import quote_found

ABSTRACT = (
    "Imatinib potently inhibited ABL1 kinase activity in vitro. In patients with chronic "
    "myeloid leukemia, imatinib induced durable responses — most within 12 months."
)


def test_verbatim_quote_found_despite_case_space_and_dashes():
    assert quote_found("imatinib  potently inhibited ABL1 kinase activity", ABSTRACT)
    assert quote_found("induced durable responses - most within 12 months", ABSTRACT)
    assert quote_found("Imatinib potently inhibited ABL1 kinase activity.", "", ABSTRACT)


def test_paraphrase_is_not_found():
    assert not quote_found("imatinib strongly inhibited ABL1 kinase activity", ABSTRACT)
    assert not quote_found("", ABSTRACT) and not quote_found(None, ABSTRACT)


def test_ellipsis_parts_must_each_be_long_and_in_order():
    joined = "imatinib potently inhibited ABL1 ... imatinib induced durable responses"
    assert quote_found(joined, ABSTRACT)
    assert quote_found(joined.replace("...", "…"), ABSTRACT)
    assert quote_found(joined.replace("...", "[...]"), ABSTRACT)
    # Two entity names joined by an ellipsis are not evidence.
    assert not quote_found("imatinib ... leukemia", ABSTRACT)
    assert not quote_found(
        "imatinib induced durable responses ... imatinib potently inhibited ABL1", ABSTRACT
    )


def test_short_quote_rejected():
    assert not quote_found("imatinib inhibited", "imatinib inhibited")


def test_at_most_three_parts():
    text = " ".join(f"sentence number {i} of the abstract is here." for i in range(5))
    parts = [f"sentence number {i} of the abstract" for i in range(4)]
    assert quote_found(" ... ".join(parts[:3]), text)
    assert not quote_found(" ... ".join(parts), text)


def test_brackets_dont_count():
    abstract = "Guanidinoacetate methyltransferase deficiency (GAMT-D) is one of three syndromes."
    assert quote_found("GAMT-D is one of three syndromes", abstract)
