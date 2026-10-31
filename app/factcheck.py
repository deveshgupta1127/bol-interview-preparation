"""Check that a suggested answer adds no facts: every number and every name/tool in it must come from the
student's resume or their own answer. Claude writes the rewrite; this check runs after it."""
import re

_COMMON = {
    "i", "i'm", "i've", "i'd", "my", "in", "at", "the", "a", "an", "and", "so", "then", "first", "second", "third",
    "finally", "after", "before", "when", "while", "during", "as", "this", "that", "these", "those", "we", "our",
    "it", "there", "here", "also", "because", "to", "for", "with", "on", "by", "from", "of", "one", "since",
    "situation", "task", "action", "result", "overall", "today", "yes", "no", "thank", "thanks", "hello", "hi",
    "sir", "ma'am", "madam", "english", "hindi", "monday", "tuesday", "wednesday", "thursday", "friday",
}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_TERM = re.compile(r"\b(?:[A-Z][\w+#.]*[A-Za-z0-9+#]|[a-z]+[A-Z][\w]*|\w*\d\w*[A-Za-z]\w*)")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", s.lower())


def unsupported(rewrite: str, *sources: str) -> list[str]:
    source = " ".join(sources)
    source_numbers = {n.replace(",", "") for n in _NUMBER.findall(source)}
    source_words = {_norm(w) for w in re.findall(r"[\w+#.]+", source)}
    source_flat = _norm(source)
    missing: list[str] = []

    for n in _NUMBER.findall(rewrite):
        n = n.replace(",", "")
        if n not in source_numbers and n not in missing:
            missing.append(n)

    for term in _TERM.findall(rewrite):
        key = _norm(term)
        if not key or key in _COMMON or key.isdigit():
            continue
        if key in source_words or key in source_flat:
            continue
        if term not in missing:
            missing.append(term)
    return missing
