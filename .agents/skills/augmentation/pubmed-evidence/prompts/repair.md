# Repairing a quote

A first-pass model read a PubMed abstract and wrote down a claim it makes about two entities
of a biomedical knowledge graph, with a sentence from the abstract as its evidence. Code could
not find that sentence in the abstract: the model may have paraphrased, shortened or joined
parts of it. You find the passage the first pass meant.

The message gives the claim (as a sentence, with its polarity, and in structured form: the
statement, what else it supports, its role, whether it is hedged, its context), the evidence
sentence that was not found, and the abstract with its title. A negative claim says the
abstract reports that the statement does not hold.

Give `quote`: the passage of the title or abstract that states the finding the first pass's
claim rests on, copied character for character: the same words, spelling, abbreviations,
numbers and punctuation. Quote at least a full clause, usually one sentence and at most two; if
the finding is split, join two spans of the same abstract, in order, with `...`. The passage
must name both entities, by a name or synonym, or join the span that names them. Don't
paraphrase, shorten, expand abbreviations or fix typos.

If no passage states that finding, give `null`. That is a normal answer, not a failure: the
first pass may have claimed something the abstract doesn't say, and then its claim should lose
its quote. A wrong quote is worse than none, so don't pick a passage that is only about the
same topic or that states something else (another entity, the opposite direction, a
hypothesis, a weaker claim). You only supply the quote; you don't change the claim.
