"""Deep-link slug rules.

The slug is the payload of ``https://t.me/<bot>?start=<slug>``, so it must obey
Telegram's constraints: 1-64 characters from ``A-Z a-z 0-9 _ -``.
"""

from __future__ import annotations

import re
from typing import Final

from app.core.exceptions import InvalidSlugError

SLUG_MAX_LENGTH: Final = 64
SLUG_PATTERN: Final = r"^[A-Za-z0-9_-]{1,64}$"
_SLUG_RE: Final = re.compile(SLUG_PATTERN)


def is_valid_slug(value: str) -> bool:
    """Return ``True`` when the value can be used as a deep-link payload."""
    return bool(_SLUG_RE.match(value))


def normalise_slug(value: str) -> str:
    """Strip surrounding whitespace and validate the slug.

    Raises:
        InvalidSlugError: the value cannot be used in a Telegram deep link.
    """
    candidate = value.strip()
    if not is_valid_slug(candidate):
        message = (
            f"Slug must match {SLUG_PATTERN}: 1-{SLUG_MAX_LENGTH} characters "
            f"from A-Z, a-z, 0-9, underscore and hyphen"
        )
        raise InvalidSlugError(message, slug=value)
    return candidate
