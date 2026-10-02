"""Place-name normalisation shared by the gazetteer build, the loaders and lookups.

Every code path that turns a human-typed place name into a lookup key MUST go
through :func:`normalize_place_name`; otherwise keys written at build time will
not match keys computed at request time.
"""

import re
import unicodedata

US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

_STATE_NAME_TO_CODE = {name.upper(): code for code, name in US_STATES.items()}

# Leading-token abbreviations that the fuel CSV and gazetteers spell inconsistently.
_TOKEN_ALIASES = {"SAINT": "ST", "SAINTE": "STE", "FORT": "FT", "MOUNT": "MT"}

# Non-ASCII punctuation (e.g. typographic apostrophes) is already dropped by the ASCII fold.
_PUNCTUATION = re.compile(r"[.'`]")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def normalize_place_name(name: str) -> str:
    """Return the canonical lookup key for a place name.

    ASCII-folds, upper-cases, canonicalises common abbreviations and removes all
    separators, so ``"Mc Calla"``, ``"McCalla"`` and ``"MCCALLA"`` collapse to the
    same key, as do ``"St. Louis"`` and ``"Saint Louis"``.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    cleaned = _NON_ALNUM.sub(" ", _PUNCTUATION.sub("", folded.upper())).strip()
    tokens = [_TOKEN_ALIASES.get(token, token) for token in cleaned.split()]
    return "".join(tokens)


def normalize_state(value: str) -> str | None:
    """Return the two-letter USPS code for a state code or full state name."""
    candidate = " ".join(value.replace(".", "").upper().split())
    if candidate in US_STATES:
        return candidate
    return _STATE_NAME_TO_CODE.get(candidate)
