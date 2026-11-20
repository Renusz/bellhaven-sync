"""Normalization: make two spellings of the same thing compare equal.

"700 Carpenter Rd" and "700 Carpenter Road", "W Main St" and "West Main Street",
"(330) 431-2495" and "330.431.2495" are the same facts written differently. Matching
compares the normalized forms; proposals always carry the original text.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

STREET_SUFFIX = {
    "street": "st", "st": "st", "str": "st",
    "road": "rd", "rd": "rd",
    "avenue": "ave", "ave": "ave", "av": "ave",
    "boulevard": "blvd", "blvd": "blvd",
    "drive": "dr", "dr": "dr",
    "lane": "ln", "ln": "ln",
    "pike": "pike", "pk": "pike", "pke": "pike",
    "parkway": "pkwy", "pkwy": "pkwy",
    "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl",
    "circle": "cir", "cir": "cir",
    "highway": "hwy", "hwy": "hwy",
    "terrace": "ter", "ter": "ter",
    "trail": "trl", "trl": "trl",
    "square": "sq", "sq": "sq",
}
DIRECTION = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "n": "n", "s": "s", "e": "e", "w": "w", "ne": "ne", "nw": "nw", "se": "se", "sw": "sw",
}
# Words that don't identify a facility, and spelling variants that mean the same thing.
NAME_STOPWORDS = {"the", "of", "at", "and", "a", "in"}
NAME_SYNONYMS = {"rehab": "rehabilitation", "centre": "center", "healthcare": "health care", "ctr": "center"}

PO_BOX = re.compile(r"^\s*p\.?\s*o\.?\s*box\b", re.I)


def ascii_fold(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def words(text: str | None) -> list[str]:
    text = ascii_fold(text).lower().replace("&", " and ")
    return re.findall(r"[a-z0-9]+", text)


def street_number(street: str | None) -> str | None:
    match = re.match(r"\s*(\d+)", street or "")
    return match.group(1) if match else None


def is_po_box(street: str | None) -> bool:
    return bool(PO_BOX.match(street or ""))


def norm_street(street: str | None) -> str:
    """'4850 Northwest Sylvania Avenue' -> '4850 nw sylvania ave'."""
    out = []
    for w in words(street):
        out.append(DIRECTION.get(w) or STREET_SUFFIX.get(w) or w)
    return " ".join(out)


def norm_city(city: str | None) -> str:
    return " ".join(words(city))


def norm_state(state: str | None) -> str:
    return (state or "").strip().upper()


def norm_zip(zipcode: str | None) -> str:
    return re.sub(r"\D", "", zipcode or "")[:5]


def norm_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def norm_name(name: str | None) -> str:
    """'The Arbors at Bellhaven - Dayton' -> 'arbors bellhaven dayton'."""
    out = []
    for w in words(name):
        w = NAME_SYNONYMS.get(w, w)
        out.extend(x for x in w.split() if x not in NAME_STOPWORDS)
    return " ".join(out)


def name_similarity(a: str | None, b: str | None, ignore: set[str] | frozenset = frozenset()) -> float:
    """0..1. The max of character similarity and word overlap, so both typos and reordering score well.

    `ignore` drops brand words first, so "Bellhaven of Marion" and "Bellhaven of Toledo" don't look alike
    just because both say "Bellhaven".
    """
    na = " ".join(w for w in norm_name(a).split() if w not in ignore)
    nb = " ".join(w for w in norm_name(b).split() if w not in ignore)
    if not na or not nb:
        return 0.0
    chars = SequenceMatcher(None, na, nb).ratio()
    sa, sb = set(na.split()), set(nb.split())
    overlap = len(sa & sb) / len(sa | sb)
    return max(chars, overlap)


def same_name(a: str | None, b: str | None) -> bool:
    """True when two names differ only in spelling conventions (case, '&' vs 'and', 'Centre' vs 'Center', 'the', ...)."""
    return norm_name(a) == norm_name(b)


def care_type(offering: str | None, mapping: dict[str, str]) -> str | None:
    return mapping.get((offering or "").strip().lower())
