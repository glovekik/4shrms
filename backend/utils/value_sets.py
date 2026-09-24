"""Controlled vocabularies — the one place a fixed set of values is defined.

These were free text everywhere, and production shows exactly what that
costs. Eighteen people produced seven spellings of a blood group ("B+",
"o+", "B-positive", "A +ve"), five of a gender including the typo "Manle",
and six of a marital status including "Single :(". A blood group on an ID
card exists for an emergency; "B-positive" cannot be read with confidence
as either B+ or B-.

Defined server-side rather than in the app so the API and every client
agree, and exposed over /meta/value-sets so the app renders the options it
will actually be validated against instead of keeping its own copy.

`normalize` accepts what people have already typed and maps it to the
canonical value. It is deliberately conservative: only unambiguous variants
are coerced, and anything else is rejected so a wrong guess never silently
becomes someone's medical detail.
"""

from typing import Optional

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"]
GENDERS = ["Male", "Female", "Other", "Prefer not to say"]
MARITAL_STATUSES = ["Single", "Married", "Divorced", "Widowed"]
RELATIONSHIPS = [
    "Mother", "Father", "Spouse", "Sibling", "Child", "Friend", "Other",
]

# Spellings already in the database, or ones people plainly mean. Keys are
# compared lowercased with spaces and dots stripped.
_ALIASES: dict[str, dict[str, str]] = {
    "bloodGroup": {
        "apositive": "A+", "aposve": "A+", "ave": "A+", "a+ve": "A+",
        "bpositive": "B+", "bposve": "B+", "b+ve": "B+",
        "opositive": "O+", "oposve": "O+", "o+ve": "O+",
        "abpositive": "AB+", "ab+ve": "AB+",
        "anegative": "A-", "a-ve": "A-",
        "bnegative": "B-", "b-ve": "B-",
        "onegative": "O-", "o-ve": "O-",
        "abnegative": "AB-", "ab-ve": "AB-",
    },
    "gender": {
        "m": "Male", "f": "Female",
        "man": "Male", "woman": "Female",
    },
    "maritalStatus": {
        "unmarried": "Single", "notmarried": "Single", "bachelor": "Single",
        "spinster": "Single",
    },
}

_SETS: dict[str, list[str]] = {
    "bloodGroup": BLOOD_GROUPS,
    "gender": GENDERS,
    "maritalStatus": MARITAL_STATUSES,
    "relationship": RELATIONSHIPS,
}


def _key(value: str) -> str:
    return "".join(value.split()).replace(".", "").lower()


def options(name: str) -> list[str]:
    """The canonical values for a set, in display order."""
    return list(_SETS.get(name, []))


def normalize(name: str, value: Optional[str]) -> Optional[str]:
    """Canonical value, or None when there's nothing to store.

    Raises ValueError when the input can't be mapped confidently — the
    caller turns that into a 400 naming the valid options, rather than
    storing a guess.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    allowed = _SETS.get(name)
    if allowed is None:
        return raw  # not a controlled field

    # Exact match, case-insensitively ("o+" -> "O+", "FEMALE" -> "Female").
    for canonical in allowed:
        if _key(canonical) == _key(raw):
            return canonical

    alias = _ALIASES.get(name, {}).get(_key(raw))
    if alias:
        return alias

    raise ValueError(
        f"{raw!r} is not a valid {name}. Choose one of: "
        + ", ".join(allowed)
    )


def all_sets() -> dict[str, list[str]]:
    """Everything the client needs to render its dropdowns."""
    return {name: list(values) for name, values in _SETS.items()}
