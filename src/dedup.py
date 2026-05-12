"""Cross-platform job deduplication: hash of company name + normalized title + location.

Problems solved:
  - Same job posted on LinkedIn + Indeed + Dice → 3 duplicates
  - Same company posts same job with different title formats ("ML Engineer" vs "Machine Learning Engineer")

Hash strategy:
  content_hash = sha256(normalize(company) + "|" + normalize(title) + "|" + normalize(location))[:16]

If three fields are identical after normalization, jobs from different platforms/URLs are considered duplicates.

Normalization rules:
  company:  lowercase, remove legal suffixes (Inc/Corp/LLC...), remove punctuation
  title:    lowercase, remove seniority prefixes (Senior/Lead/Staff...), normalize common abbrevs (ML/SRE/SWE...), remove level suffixes (L5/E4/I/II)
  location: lowercase, normalize city aliases (SF→san francisco), keep only city name
"""
from __future__ import annotations

import hashlib
import re


# ── Company name normalization ─────────────────────────────────────────────────────────────

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|corp|ltd|llc|co|company|companies|technologies|technology|"
    r"tech|labs|laboratory|laboratories|ai|systems|solutions|group|"
    r"holdings|ventures|global|international|services|software|"
    r"consulting|consulting|networks|network|media|studio|studios)\b\.?",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def normalize_company(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = _COMPANY_SUFFIXES.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s


# ── Job title normalization ─────────────────────────────────────────────────────────────

# Seniority/level prefixes (remove)
_SENIORITY_PREFIX = re.compile(
    r"^(senior|sr\.?|junior|jr\.?|lead|staff|principal|associate|assoc\.?|"
    r"distinguished|fellow|founding|head of|director of|vp of|"
    r"entry.?level|mid.?level)\s+",
    re.IGNORECASE,
)

# Level suffixes: (L5), E4, I, II, III, IV, 1, 2, 3
_LEVEL_SUFFIX = re.compile(
    r"\s*[\(\[]?(l\d|e\d|m\d|t\d|\biv\b|\biii\b|\bii\b|\bi\b|\b[1-6]\b)[\)\]]?\s*$",
    re.IGNORECASE,
)

# Parenthetical content (usually clarifications or levels)
_PARENS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")

# Common abbreviations → expanded form
_ABBREV: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bml\b",  re.I), "machine learning"),
    (re.compile(r"\bmle\b", re.I), "machine learning engineer"),
    (re.compile(r"\bsre\b", re.I), "site reliability engineer"),
    (re.compile(r"\bswe\b", re.I), "software engineer"),
    (re.compile(r"\bsde\b", re.I), "software development engineer"),
    (re.compile(r"\bai\b",  re.I), "artificial intelligence"),
    (re.compile(r"\bnlp\b", re.I), "natural language processing"),
    (re.compile(r"\bcv\b",  re.I), "computer vision"),
    (re.compile(r"\bllm\b", re.I), "large language model"),
    (re.compile(r"\binfra\b", re.I), "infrastructure"),
    (re.compile(r"\bdev\b",   re.I), "developer"),
    (re.compile(r"\beng\b",   re.I), "engineer"),
    (re.compile(r"\bmts\b",   re.I), "member technical staff"),
]


def normalize_title(title: str) -> str:
    if not title:
        return ""
    s = title.strip()
    # Remove parenthetical content
    s = _PARENS.sub("", s)
    # Remove level suffixes
    s = _LEVEL_SUFFIX.sub("", s)
    # Remove seniority prefixes (loop until none remain)
    for _ in range(3):
        new = _SENIORITY_PREFIX.sub("", s).strip()
        if new == s:
            break
        s = new
    # Lowercase
    s = s.lower()
    # Normalize abbreviations
    for pattern, replacement in _ABBREV:
        s = pattern.sub(replacement, s)
    # Remove excess whitespace and punctuation
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s


# ── Location normalization ────────────────────────────────────────────────────────────────

# City aliases → standard names
_CITY_ALIASES: dict[str, str] = {
    "sf":              "san francisco",
    "bay area":        "san francisco",
    "silicon valley":  "san francisco",
    "nyc":             "new york",
    "new york city":   "new york",
    "ny":              "new york",
    "la":              "los angeles",
    "socal":           "los angeles",
    "seattle":         "seattle",
    "boston":          "boston",
    "chicago":         "chicago",
    "austin":          "austin",
    "denver":          "denver",
    "dc":              "washington dc",
    "washington dc":   "washington dc",
}

# Remove state/country suffixes
_LOCATION_CLEANUP = re.compile(
    r",?\s*(ca|ny|wa|tx|ma|il|co|va|ga|fl|or|nc|az|mn|oh|"
    r"california|new york|washington|texas|massachusetts|illinois|"
    r"colorado|virginia|georgia|florida|oregon|united states|usa|us)\s*$",
    re.IGNORECASE,
)


def normalize_location(location: str) -> str:
    if not location:
        return "unknown"
    s = location.lower().strip()
    # Standardize remote directly
    if re.search(r"\bremote\b", s):
        return "remote"
    # Remove state/country suffixes
    s = _LOCATION_CLEANUP.sub("", s).strip(" ,")
    # City aliases
    for alias, standard in _CITY_ALIASES.items():
        if s == alias or s.startswith(alias + " ") or s.startswith(alias + ","):
            return standard
    # Keep only part before first comma (city)
    s = s.split(",")[0].strip()
    return s or "unknown"


# ── Final hash ─────────────────────────────────────────────────────────────────

def content_hash(title: str, company: str, location: str) -> str:
    """Return 16-character hex hash for cross-platform deduplication.

    Same company + same job + same location → same hash, regardless of platform.
    """
    nc = normalize_company(company)
    nt = normalize_title(title)
    nl = normalize_location(location)
    key = f"{nc}|{nt}|{nl}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def dedup_key(title: str, company: str, location: str) -> str:
    """Return human-readable dedup key (for debugging/logs)."""
    return (
        f"{normalize_company(company)}"
        f" | {normalize_title(title)}"
        f" | {normalize_location(location)}"
    )
