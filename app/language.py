"""Transcript checks that don't need an LLM: fillers, repeats, Hindi/English mix, and common Indian-English phrasing.

Prisma's verbatim mode keeps fillers, but Hindi words (and sometimes English loanwords) come back in Devanagari,
so every list has both scripts.
"""
import re

# English fillers. "like" is handled separately because it is usually a real word.
_FILLERS_LATIN = ["um", "umm", "ummm", "uh", "uhh", "uhm", "er", "erm", "hmm", "hmmm", "ah", "basically", "actually",
                  "literally", "you know", "i mean", "kind of", "sort of", "matlab", "yaani", "yani", "achha", "acha",
                  "kya bolte", "kya kehte"]
_FILLERS_DEVANAGARI = ["उम्म", "उम", "अं", "अम्म", "हम्म", "मतलब", "यानी", "यानि", "अच्छा", "बेसिकली", "एक्चुअली",
                       "क्या बोलते", "क्या कहते"]

# Phrases that sound fine in everyday Indian English but read as errors to many interviewers.
# (pattern, better phrasing, short reason)
INDIAN_ENGLISH = [
    (r"\bmyself\s+(?=[A-Z][a-z])", "I'm ", "In introductions say \"I'm Rahul\", not \"Myself Rahul\"."),
    (r"\brevert(ed)? back\b", "get back", "\"Revert back\" is usually \"get back to you\" or \"reply\"."),
    (r"\breturn(ed)? back\b", "return", "\"Return\" already means go back."),
    (r"\bdo the needful\b", "do what's needed", "\"Do the needful\" sounds dated; say what you will actually do."),
    (r"\bi am having\b(?! (?:fun|lunch|dinner|breakfast|tea|a (?:good|great) time))", "I have",
     "Use \"I have\" for experience, skills and knowledge."),
    (r"\bprepone(d)?\b", "move earlier", "\"Prepone\" isn't used outside India; say \"move it earlier\"."),
    (r"\bdiscuss(ed)? about\b", "discuss", "\"Discuss\" takes no \"about\"."),
    (r"\bexplain(ed)? about\b", "explain", "\"Explain\" takes no \"about\"."),
    (r"\bcousin (brother|sister)\b", "cousin", "\"Cousin\" alone is enough."),
    (r"\bpass(ed)? out (of|from)\b", "graduated from", "\"Passed out\" means fainted; say \"graduated from\"."),
    (r"\bsince (\d+|one|two|three|four|five|six) (years|months)\b", r"for \1 \2",
     "Use \"for\" with a length of time, \"since\" with a starting point."),
    (r"\bout of station\b", "out of town", "Say \"out of town\"."),
    (r"\bgood name\b", "name", "In English just ask or give your \"name\"."),
]

_WORD = re.compile(r"[A-Za-z']+|[ऀ-ॿ]+|\d+")


def words(text: str) -> list[str]:
    return _WORD.findall(text)


def _count_phrase(text_lower: str, phrase: str, latin: bool) -> int:
    if latin:
        return len(re.findall(r"(?<![a-z'])" + re.escape(phrase) + r"(?![a-z'])", text_lower))
    # \b doesn't work with Devanagari vowel signs, so check neighbours by hand.
    return len(re.findall(r"(?<![ऀ-ॿ])" + re.escape(phrase) + r"(?![ऀ-ॿ])", text_lower))


def fillers(text: str) -> dict[str, int]:
    lower = text.lower()
    found: dict[str, int] = {}
    for phrase in _FILLERS_LATIN:
        n = _count_phrase(lower, phrase, latin=True)
        if n:
            found[phrase] = found.get(phrase, 0) + n
    for phrase in _FILLERS_DEVANAGARI:
        n = _count_phrase(lower, phrase, latin=False)
        if n:
            found[phrase] = found.get(phrase, 0) + n
    # Filler "like": set off by commas or next to another filler ("I was, like, nervous" / "like um").
    like = len(re.findall(r"(?:^|[,.]\s*)like\s*,|,\s*like\b(?!\s+(?:to|a|the|this|that)\b)|\blike\s+(?:um|uh)\b",
                          lower))
    if like:
        found["like"] = like
    # "um" inside "umm" would double count; the patterns are whole-word so this cannot happen.
    return found


def repeats(text: str) -> int:
    """Stutter-style repeats: "I I", "the the", "we we we" (counts each extra copy)."""
    tokens = [w.lower() for w in words(text)]
    return sum(1 for a, b in zip(tokens, tokens[1:]) if a == b and not a.isdigit())


def english_share(text: str) -> float | None:
    """Share of words in Latin script. Hindi words (and Devanagari loanwords) lower it."""
    tokens = [w for w in words(text) if not w.isdigit()]
    if not tokens:
        return None
    latin = sum(1 for w in tokens if w[0].isascii())
    return round(100 * latin / len(tokens), 1)


def indian_english(text: str) -> list[dict]:
    hits = []
    for pattern, better, why in INDIAN_ENGLISH:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            hits.append({"said": m.group(0).strip(), "better": m.expand(better).strip(), "why": why})
    return hits


def polish(text: str) -> str:
    """Light clean-up without an LLM: drop fillers and repeats, fix the phrases above. Adds nothing new."""
    for phrase in sorted(_FILLERS_LATIN + _FILLERS_DEVANAGARI, key=len, reverse=True):
        text = re.sub(r"(?<![\w'])" + re.escape(phrase) + r"(?![\w'])\s*,?", "", text, flags=re.I)
    text = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", text, flags=re.I)
    for pattern, better, _ in INDIAN_ENGLISH:
        text = re.sub(pattern, better, text, flags=re.I)
    text = re.sub(r"\s*,(\s*,)+", ",", text)
    text = re.sub(r"\s+([,.])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,")
    # capitalise each sentence start, skipping any leftover commas
    return re.sub(r"(^|[.!?]\s+)[,\s]*(\w)", lambda m: m.group(1) + m.group(2).upper(), text)


def analyse(text: str, seconds: float | None, hesitations: int = 0) -> dict:
    """`seconds` is the whole answer, pauses included (how interview pace is usually measured).
    `hesitations` = "umm"/"uh" sounds counted from the audio (the transcript drops them); they count as fillers."""
    found = fillers(text)
    count = sum(found.values())
    total = len(words(text))
    minutes = (seconds or 0) / 60
    return {
        "words": total,
        "fillers": found,
        "filler_count": count,
        "hesitations": hesitations,
        "fillers_per_min": round((count + hesitations) / minutes, 1) if minutes >= 0.05 else None,
        "repeats": repeats(text),
        "english_share": english_share(text),
        "indian_english": indian_english(text),
        "wpm": round(total / minutes) if minutes >= 0.05 else None,
    }
