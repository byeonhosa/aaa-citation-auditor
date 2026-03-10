"""Legal case name normalisation and fuzzy matching.

Legal citations routinely abbreviate party names (Bluebook Rule 10.2.1).
This module normalises case names to a canonical token set so that equivalent
names such as "Police Department of Chicago" and "Police Dept. of Chicago"
compare as equal.

Public API
----------
normalize_case_name(name)  → str
    Canonical lowercase token string, suitable for exact comparison.

case_names_match(name_a, name_b)  → tuple[bool, float]
    (matched, confidence) where confidence is 1.0 (exact), 0.8 (subset), or
    0.0 (no match).

Design principles
-----------------
* Conservative — false positives (matching different cases) are far worse than
  false negatives.  The subset rule requires ALL significant tokens from the
  shorter name to appear in the longer one.
* No external libraries — pure string operations only.
* Abbreviation mapping is bidirectional: both the abbreviated form and the
  expanded form are normalised to the same canonical token.
"""

from __future__ import annotations

import re

# ── Abbreviation table ────────────────────────────────────────────────────────
#
# Each entry: (canonical_token, [variants_to_replace])
#
# Variants are matched case-insensitively as whole words (i.e. surrounded by
# word boundaries).  The canonical form is used in the normalised output so
# that "Dept." and "Department" both produce the same canonical token "dept".
#
# Keep entries sorted by canonical token for readability.

_ABBREV_TABLE: list[tuple[str, list[str]]] = [
    ("admin", ["administration", "admin"]),
    ("assn", ["association", "ass'n", "assn", "assoc"]),
    ("auth", ["authority", "auth"]),
    ("bd", ["board", "bd"]),
    ("bldg", ["building", "bldg"]),
    ("bros", ["brothers", "bros"]),
    ("ch", ["chapter", "ch"]),
    ("cir", ["circuit", "cir"]),
    ("cnty", ["county", "cnty", "cty", "co"]),
    ("comm", ["commission", "comm", "comn"]),
    ("commr", ["commissioner", "commr", "comr"]),
    ("corp", ["corporation", "corp"]),
    ("dept", ["department", "dept"]),
    ("dist", ["district", "dist"]),
    ("div", ["division", "div"]),
    ("emps", ["employees", "emps", "emples"]),
    ("enters", ["enterprises", "enters", "enter"]),
    ("fedn", ["federation", "fedn", "fed"]),
    ("govt", ["government", "gov't", "govt"]),
    ("grp", ["group", "grp"]),
    ("hosp", ["hospital", "hosp"]),
    ("hr", ["human resources", "hr"]),
    ("inc", ["incorporated", "inc"]),
    ("indus", ["industries", "industry", "indus"]),
    ("ins", ["insurance", "ins"]),
    ("intl", ["international", "intl", "int'l"]),
    ("lab", ["laboratory", "laboratories", "lab"]),
    ("ltd", ["limited", "ltd"]),
    ("mgmt", ["management", "mgmt"]),
    ("mfg", ["manufacturing", "mfg"]),
    ("mkt", ["marketing", "mkt"]),
    ("mun", ["municipal", "municipality", "mun"]),
    ("natl", ["national", "natl", "nat'l"]),
    ("org", ["organization", "organisation", "org"]),
    ("prods", ["products", "prods", "prod"]),
    ("ry", ["railway", "railroad", "ry", "rr"]),
    ("sch", ["school", "sch"]),
    ("servs", ["services", "service", "servs", "serv"]),
    ("sys", ["systems", "system", "sys"]),
    ("tech", ["technology", "technologies", "tech"]),
    ("telecom", ["telecommunications", "telecom"]),
    ("transp", ["transportation", "transp"]),
    ("twp", ["township", "twp"]),
    ("univ", ["university", "univ"]),
    ("util", ["utilities", "utility", "util"]),
]

# Build a compiled regex that replaces each variant with its canonical token.
# Order matters: longer variants must be tried before shorter ones to avoid
# partial replacements (e.g. "association" before "assoc" before "ass'n").
_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = []
for _canon, _variants in _ABBREV_TABLE:
    # Sort longest-first so greedy matching picks the most specific form
    for _variant in sorted(_variants, key=len, reverse=True):
        # Escape the variant for regex (handles "ass'n", "gov't", etc.)
        _pat = re.compile(r"\b" + re.escape(_variant) + r"\b", re.IGNORECASE)
        _REPLACEMENTS.append((_pat, _canon))

# Titles / honorifics to strip entirely
_TITLE_RE = re.compile(
    r"\b(mr|mrs|ms|miss|dr|prof|rev|hon|sr|jr)\.?\b",
    re.IGNORECASE,
)

# Punctuation to remove after abbreviation expansion
_PUNCT_RE = re.compile(r"[^\w\s]")

# Filler words to strip (after abbrev expansion and punctuation removal)
_FILLER_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)

# Collapse whitespace
_SPACES_RE = re.compile(r"\s+")


# ── Public helpers ────────────────────────────────────────────────────────────


def normalize_case_name(name: str) -> str:
    """Return the canonical normalised form of a case name.

    Steps
    -----
    1. Strip titles / honorifics ("Mr.", "Dr.", …).
    2. Expand / collapse abbreviations using the abbreviation table.
    3. Remove remaining punctuation.
    4. Lowercase.
    5. Remove filler words ("of", "the", "and", …).
    6. Collapse whitespace and strip.

    The result is a space-separated string of canonical tokens, suitable for
    direct equality comparison.
    """
    text = name

    # 1. Remove honorifics
    text = _TITLE_RE.sub(" ", text)

    # 2. Expand/collapse abbreviations
    for pattern, canonical in _REPLACEMENTS:
        text = pattern.sub(canonical, text)

    # 3. Remove punctuation (periods, commas, apostrophes, etc.)
    text = _PUNCT_RE.sub(" ", text)

    # 4. Lowercase
    text = text.lower()

    # 5. Remove filler words (whole-word match on already-lowercased text)
    tokens = _SPACES_RE.split(text.strip())
    tokens = [t for t in tokens if t and t not in _FILLER_WORDS]

    # 6. Collapse back to string
    return " ".join(tokens)


def _significant_tokens(normalised: str) -> list[str]:
    """Return tokens with 3+ characters from a normalised name string."""
    return [t for t in normalised.split() if len(t) >= 3]


def case_names_match(name_a: str, name_b: str) -> tuple[bool, float]:
    """Compare two case names using legal-citation-aware normalisation.

    Returns
    -------
    (matched, confidence)
        matched    – True if the names refer to the same case with sufficient
                     confidence, False otherwise.
        confidence – 1.0  exact match after normalisation
                     0.8  subset match (all significant tokens of the shorter
                          name appear in the longer name)
                     0.0  no match

    Rules (conservative — designed to avoid false positives)
    --------------------------------------------------------
    1. Exact match: normalised(A) == normalised(B) → confidence 1.0
    2. Subset match: ALL significant tokens (3+ chars) from the shorter
       normalised name appear in the longer normalised name → confidence 0.8.
       Requires at least 2 significant tokens in the shorter name to avoid
       spurious single-word matches.
    3. Otherwise: confidence 0.0 (no match).
    """
    norm_a = normalize_case_name(name_a)
    norm_b = normalize_case_name(name_b)

    # Guard: empty normalised names never match
    if not norm_a or not norm_b:
        return False, 0.0

    # 1. Exact match
    if norm_a == norm_b:
        return True, 1.0

    # 2. Subset match
    tokens_a = _significant_tokens(norm_a)
    tokens_b = _significant_tokens(norm_b)

    if not tokens_a or not tokens_b:
        return False, 0.0

    # Determine which is shorter (fewer significant tokens)
    if len(tokens_a) <= len(tokens_b):
        shorter_tokens, longer_norm = tokens_a, norm_b
    else:
        shorter_tokens, longer_norm = tokens_b, norm_a

    # Require at least 2 significant tokens in the shorter name
    if len(shorter_tokens) < 2:
        return False, 0.0

    longer_token_set = set(longer_norm.split())
    if all(t in longer_token_set for t in shorter_tokens):
        return True, 0.8

    return False, 0.0
