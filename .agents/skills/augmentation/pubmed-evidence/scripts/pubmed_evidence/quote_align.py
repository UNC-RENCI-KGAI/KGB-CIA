"""The code half of step 3's quote repair: put back, in code, an evidence sentence the label
model misquoted. Standard library only, and no model.

When the bulk model's quote isn't in its abstract (`quotes.quote_found`), it is almost always
the abstract's own words, slightly off: sentences stitched together without '...', words
dropped, one to three words changed, punctuation, hyphens or a 'Title:' prefix (93% of the 409
failures measured on 2026-11-16). So `repair` finds the sentences the quote was taken from and
quotes them whole:

1. Split the title and the abstract into sentences, keeping each one's offsets so that a choice
   is cut verbatim from the text (`split_sentences`).
2. Cover the quote's words with sentences (`_cover`): first the sentence holding most of them in
   order (difflib's matching blocks), then a second if it adds at least MIN_GAIN words the first
   didn't hold. The title and the abstract are covered separately, since a quote can't mix them.
3. Under FLOOR of the quote's words covered, the quote isn't in the abstract (a made-up or
   heavily rewritten sentence): return None, and the repair model gets the claim instead.
4. Prefer the source whose passage names both entities, else the better covered.
5. If the passage doesn't name both entities, join with '...' a sentence that names the missing
   one, as the label prompt's own rule asks: preferring one that names both, then the one
   sharing most words with the claim, then the nearest before the passage. With none, the
   passage is kept as it is and `info['unresolved']` says so.

The result is whole sentences of the abstract's own text, at most MAX_PARTS runs of at least
MIN_PART_CHARS each, so `quote_found` accepts it by construction (`repair.py` checks anyway).
`repair.py` stores what it returns under the repairer 'code' and VERSION.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from pubmed_evidence.quotes import _ELLIPSIS, MAX_PARTS, MIN_PART_CHARS, normalize

# The repair version code repairs are cached under (`claim_repairs.repair_prompt_version`, with
# the repairer 'code'). Bump it on any change to what `repair` returns: repairs made under
# another version are made again.
VERSION = "align-2"
# Least share of the quote's (weighted) words the passage must cover, else no repair.
FLOOR = 0.5
# Most sentences the cover takes, and the fewest new words a second one must add.
MAX_SENTS = 2
MIN_GAIN = 4
# Weight of a stopword in the cover, against 1 for any other word.
STOP_WEIGHT = 0.3

STOP = frozenset(
    """a an the of in on at to for from by with and or but not no nor as is are was were be been
    being this that these those it its their there which who whom whose than then also both
    such into onto over under between among within without via per we our they he she his her
    has have had do does did can could may might will would should shall must""".split()
)
# Words an entity's name may leave out in the text ('Behcet syndrome' named as 'Behcet').
GENERIC = frozenset(
    """disease disorder disorders syndrome gene protein human type familial obsolete condition
    deficiency form acid sodium hydrochloride potassium calcium salt""".split()
)
# A structured abstract's section label: 'RESULTS: ', 'MATERIALS AND METHODS: '.
_LABEL = re.compile(r"^([A-Z][A-Za-z /&,'-]{1,60}?):\s+")
# Words whose full stop doesn't end a sentence, in any case but all capitals: 'NO.', 'MS.' or
# 'CF.' is an acronym, which can end one.
_ABBREV = frozenset(
    """e.g i.e al vs fig figs approx no nos sp spp ca cf resp incl var inc ltd co ref refs eq
    vol pp ed eds subsp cv viz etc est""".split()
)
# Titles, abbreviations only as written here: 'ms' is milliseconds and 'DR' an acronym.
_TITLES = frozenset("Dr Mr Mrs Ms Prof Jr St".split())
# A sentence ends at '.', '!' or '?' (and a closing quote or bracket) before space and a
# capital or a digit.
_BOUNDARY = re.compile(r"[.!?]['\")\]]?\s+(?=[\"'(\[]?[A-Z0-9Α-Ω])")
_LIST_NUMBER = re.compile(r"\s*(\d{1,2}|[IVX]{1,4}|[a-z])[.)]\s*")
# An abbreviation the text defines: 'rheumatoid arthritis (RA)'.
_DEFINED = re.compile(r"([\w\-' ,/]{3,80}?)\s+\(([A-Za-z][\w\-]{1,9})\)")


@dataclass(frozen=True)
class Sentence:
    """A sentence of the title or the abstract, and where it is: `text` is
    `title_or_abstract[start:end]`, and `para` the line it is on."""

    source: str  # "title" or "abstract"
    para: int
    start: int
    end: int
    text: str


def _no_end(word: str, before: str, after: str) -> bool:
    """Whether a full stop after `word` doesn't end the sentence, `before` being the sentence up
    to the word and `after` the text from the next word on: the word is an abbreviation, a title
    or an initial. A lowercase letter is taken for an initial. A capital is one after a
    capitalized word or none ('John F. Kennedy', 'A. B. Jones'), or before another initial ('the
    M. D. Anderson'); after a lowercase word or a number it ends a name ('vitamin D.',
    'hepatitis B.', '37 C.')."""
    low = word.lower()
    if word in _TITLES or (low in _ABBREV and not (len(word) > 1 and word.isupper())):
        return True
    if re.fullmatch(r"[a-z]", word) or re.fullmatch(r"([a-z]\.)+[a-z]", low):
        return True
    if re.fullmatch(r"[A-Z]", word):
        prev = re.search(r"(\S+)\s*$", before)
        prev_word = prev.group(1).lstrip("([\"'") if prev else ""
        return not prev_word or prev_word[0].isupper() or bool(re.match(r"[A-Z]\.(\s|$)", after))
    return False


def _split_line(line: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence of one line. A full stop after an abbreviation, a title or
    an initial doesn't end one (`_no_end`), and a list number ('1.', 'ii)') stays with its
    item."""
    spans, start = [], 0
    for m in _BOUNDARY.finditer(line):
        cut = m.start() + 1
        while cut < len(line) and line[cut] in "'\")]":
            cut += 1
        sentence = line[start : m.start()]
        last = re.search(r"(\S+)$", sentence)
        word = last.group(1).lstrip("([\"'") if last else ""
        if _no_end(word, sentence[: last.start()] if last else "", line[m.end() :]):
            continue
        spans.append((start, cut))
        start = m.end()
    spans.append((start, len(line)))
    merged: list[tuple[int, int]] = []
    for s, e in (span for span in spans if line[span[0] : span[1]].strip()):
        prev = line[merged[-1][0] : merged[-1][1]] if merged else ""
        if merged and _LIST_NUMBER.fullmatch(prev):
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def split_sentences(title: str, abstract: str) -> list[Sentence]:
    """The title's sentences, then the abstract's, paragraph by paragraph (one per line), with
    a structured abstract's section label ('RESULTS: ') kept off its first sentence."""
    out = []
    for source, text in (("title", title), ("abstract", abstract)):
        offset = 0
        for para, line in enumerate(text.split("\n")):
            body = 0
            m = _LABEL.match(line) if source == "abstract" else None
            if m and len(m.group(1).split()) <= 6:
                body = m.end()
            for s, e in _split_line(line[body:]):
                seg = line[body + s : body + e]
                lead = len(seg) - len(seg.lstrip())
                seg = seg.strip()
                start = offset + body + s + lead
                out.append(Sentence(source, para, start, start + len(seg), seg))
            offset += len(line) + 1
    return out


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", normalize(text))


def _content(token: str) -> bool:
    """Whether a token carries meaning: not a stopword nor a single letter."""
    return token not in STOP and (len(token) > 1 or token.isdigit())


def _matched(quote: list[str], sentence: list[str]) -> set[int]:
    """Positions of the quote's tokens the sentence holds in order: difflib's matching blocks of
    two tokens or more, or of one content word."""
    out: set[int] = set()
    for a, _, n in SequenceMatcher(None, quote, sentence, autojunk=False).get_matching_blocks():
        if n >= 2 or (n == 1 and _content(quote[a])):
            out.update(range(a, a + n))
    return out


def _same_para(sents: list[Sentence], i: int, j: int) -> bool:
    return sents[i].source == sents[j].source and sents[i].para == sents[j].para


def _quotable(sents: list[Sentence], i: int) -> bool:
    """Whether a sentence can be quoted: it is long enough to be a part on its own, or has a
    neighbor in its paragraph to be quoted with (`_render`)."""
    if len(normalize(sents[i].text)) >= MIN_PART_CHARS:
        return True
    return any(0 <= j < len(sents) and _same_para(sents, i, j) for j in (i - 1, i + 1))


def _cover(quote: list[str], sents: list[Sentence], idx: list[int]) -> tuple[list[int], float]:
    """The sentences among `idx` that cover the quote's tokens, greedily, at most MAX_SENTS,
    and the share of the quote's weighted tokens they cover."""
    weight = [1.0 if _content(t) else STOP_WEIGHT for t in quote]
    total = sum(weight) or 1.0
    match = {
        i: (_matched(quote, _tokens(sents[i].text)) if _quotable(sents, i) else set()) for i in idx
    }
    size = {i: max(1, len(_tokens(sents[i].text))) for i in idx}
    picks: list[int] = []
    covered: set[int] = set()
    while len(picks) < MAX_SENTS:
        # Most new weight first, then the sentence the quote fills most of; the first on a tie.
        scores = {
            i: (round(sum(weight[k] for k in match[i] - covered), 6), len(match[i]) / size[i])
            for i in idx
            if i not in picks
        }
        best = max(scores, key=scores.__getitem__, default=None)
        if best is None:
            break
        new = match[best] - covered
        gain = sum(weight[k] for k in new)
        if gain == 0 or (picks and len(new) < MIN_GAIN):
            break
        picks.append(best)
        covered |= match[best]
    return picks, sum(weight[k] for k in covered) / total


def _stem(word: str) -> str:
    return word[:6] if len(word) > 6 else word


def _patterns(synonyms: Sequence[str]) -> list[set[str]]:
    """Each usable synonym as the stems of its content words, its generic words left out
    unless nothing else is left."""
    out = []
    for s in synonyms:
        if not s or "_" in s or len(s) > 80:
            continue
        words = re.findall(r"\w+", normalize(s))
        content = [x for x in words if x not in GENERIC and x not in STOP] or words
        if content and len(content) <= 6:
            out.append({_stem(x) for x in content})
    return out


def mentions(passage: str, synonyms: Sequence[str], context: str = "") -> bool:
    """Whether the passage names the entity: by a synonym's content words, stemmed and in any
    order, or by an abbreviation `context` (the whole title and abstract) defines for one of
    them ('rheumatoid arthritis (RA)')."""
    patterns = _patterns(synonyms)
    words = re.findall(r"\w+", normalize(passage))
    stems = {_stem(x) for x in words}
    if any(p <= stems for p in patterns):
        return True
    for m in _DEFINED.finditer(context or passage):
        long = {_stem(x) for x in re.findall(r"\w+", normalize(m.group(1)))}
        if any(p <= long for p in patterns) and m.group(2).lower() in words:
            return True
    return False


def _render(text_of: dict[str, str], sents: list[Sentence], picks: list[int]) -> str:
    """The picked sentences as a quote: each cut verbatim from its text, a sentence too short
    to stand alone with its neighbor, adjacent sentences of a paragraph as one run, and the
    runs, at most MAX_PARTS (those holding the first picks), joined with ' ... ' in the text's
    order."""
    chosen: set[int] = set()
    for i in picks:
        chosen.add(i)
        if len(normalize(sents[i].text)) < MIN_PART_CHARS:
            for j in (i + 1, i - 1):
                if 0 <= j < len(sents) and _same_para(sents, i, j):
                    chosen.add(j)
                    break
    runs: list[list[int]] = []
    for i in sorted(chosen):
        if runs and i == runs[-1][-1] + 1 and _same_para(sents, i, runs[-1][-1]):
            runs[-1].append(i)
        else:
            runs.append([i])
    rank = {i: n for n, i in enumerate(picks)}
    keep = sorted(runs, key=lambda run: min(rank.get(i, 99) for i in run))[:MAX_PARTS]
    text = text_of[sents[picks[0]].source]
    return " ... ".join(text[sents[r[0]].start : sents[r[-1]].end] for r in runs if r in keep)


def repair(
    quote: str,
    title: str,
    abstract: str,
    entities: Sequence[Sequence[str]] = (),
    claim: str = "",
) -> tuple[str | None, dict[str, Any]]:
    """The passage of the title or abstract `quote` was taken from, verbatim, or None when
    under FLOOR of the quote's words are in either; and what was found (`info`).

    `entities` holds each entity's names and synonyms (the subject's, then the object's: what
    the label message names them by); `claim` is the claim in words, which only ranks the
    sentences that could be joined for an entity the passage doesn't name. `info` has the
    `coverage` (0 to 1), and `joined` when a sentence was joined for an entity, or `unresolved`
    when no sentence names it."""
    sents = split_sentences(title, abstract)
    text_of = {"title": title, "abstract": abstract}
    context = title + "\n" + abstract
    words = _tokens(" ".join(p for p in _ELLIPSIS.split(quote) if p.strip()))
    if not words or not sents:
        return None, {"coverage": 0.0}

    def missing(passage: str) -> list[Sequence[str]]:
        return [e for e in entities if not mentions(passage, e, context)]

    options = []
    for source in ("abstract", "title"):
        idx = [i for i, s in enumerate(sents) if s.source == source]
        picks, coverage = _cover(words, sents, idx)
        if picks:
            both = not missing(_render(text_of, sents, picks))
            options.append(((coverage >= FLOOR, both, coverage), picks, coverage))
    if not options:
        return None, {"coverage": 0.0}
    _, picks, coverage = max(options, key=lambda option: option[0])
    info: dict[str, Any] = {"coverage": round(coverage, 3)}
    if coverage < FLOOR:
        return None, info
    miss = missing(_render(text_of, sents, picks))
    if miss:
        source, first = sents[picks[0]].source, min(picks)
        claim_words = set(_tokens(claim)) - STOP
        candidates = []
        for i, s in enumerate(sents):
            if s.source != source or i in picks or len(normalize(s.text)) < MIN_PART_CHARS:
                continue
            if any(not mentions(s.text, e, context) for e in miss):
                continue
            both = not missing(s.text)
            overlap = len(claim_words & set(_tokens(s.text)))
            candidates.append(((both, overlap, i < first, -abs(i - first)), i))
        if candidates:
            picks = [*picks, max(candidates)[1]]
            info["joined"] = True
        else:
            info["unresolved"] = True
    return _render(text_of, sents, picks), info
