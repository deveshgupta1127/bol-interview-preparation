import io
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_BYTES = 2 * 1024 * 1024
MAX_PAGES = 3


class PdfError(ValueError):
    pass


def resume_text(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise PdfError("Resume PDF must be under 2 MB.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_PAGES:
            raise PdfError("Resume must be 3 pages or fewer.")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except PdfReadError as exc:
        raise PdfError("Couldn't read that PDF. Paste the resume text instead.") from exc
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) < 200:
        raise PdfError("This PDF has almost no text (is it a scan?). Paste the resume text instead.")
    return text
