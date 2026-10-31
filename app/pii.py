"""Remove contact details and ID numbers from resumes before storing them or sending them to Claude."""
import re

_RULES = [
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[email]"),
    (re.compile(r"(https?://|www\.)\S+|\b(linkedin|github|gitlab|leetcode)\.com/\S*", re.I), "[link]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[id number]"),  # Aadhaar-style
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[id number]"),  # PAN-style
    (re.compile(r"(\+?91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b"), "[phone]"),  # 98765 43210
    (re.compile(r"(\+?91[\s-]?)?\(?\b[6-9]\d{2}\)?[\s-]?\d{3}[\s-]?\d{4}\b"), "[phone]"),  # 987-654-3210
]


def redact(text: str) -> str:
    for pattern, label in _RULES:
        text = pattern.sub(label, text)
    return text
