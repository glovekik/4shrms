"""A single, reusable email type that normalizes to lowercase.

Email addresses are case-insensitive for routing, but the app stored and
matched them verbatim — so a user created as "Gudivada@x.com" could not log in
as "gudivada@x.com" (the find_one was a case-sensitive match). Using
`NormalizedEmail` on every request model that carries an email guarantees the
value is trimmed and lowercased BEFORE it is ever stored or queried, so casing
can never split one account in two again.

Deliberately a lowercased `str`, NOT `EmailStr`: the goal is case-normalization,
and we must not start rejecting addresses that logged in fine before (EmailStr
refuses reserved TLDs like `.test`, which would 422 existing accounts). Format
validation, where wanted, is a separate concern handled at the UI.
"""

from typing import Annotated

from pydantic import BeforeValidator


def normalize_email(v):
    """Trim + lowercase. Safe on non-strings (returned unchanged)."""
    if isinstance(v, str):
        return v.strip().lower()
    return v


NormalizedEmail = Annotated[str, BeforeValidator(normalize_email)]
