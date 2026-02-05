import re
import unicodedata
from typing import Optional

_ROMANIA_REPLACEMENTS = [
    ("judetul", ""),
    ("judet", ""),
    ("jud.", ""),
    ("jud", ""),
    ("municipiul", ""),
    ("mun.", ""),
    ("sectorul", "sector"),
]

_SPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")

def strip_diacritics(s: str) -> str:
    # NFKD + remove combining marks
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))

def normalize_text(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower()
    s = strip_diacritics(s)
    for a, b in _ROMANIA_REPLACEMENTS:
        s = s.replace(a, b)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s

def safe_email(s: Optional[str]) -> str:
    if s is None:
        return ""
    return str(s).strip().lower()
