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

RESERVED_SLUG_PREFIXES: Final = ("ref_",)
"""Deep-link prefixes that mean something other than "open this product"."""


def is_valid_slug(value: str) -> bool:
    """Return ``True`` when the value can be used as a deep-link payload."""
    return bool(_SLUG_RE.match(value))


def is_reserved_slug(value: str) -> bool:
    """Whether this slug would collide with a non-product deep link.

    ``ref_…`` payloads carry an invitation, not a product, so a product must not
    be able to occupy that namespace. Only *new* slugs are checked: the
    ``/start`` handler resolves an unknown ``ref_`` code by falling through to
    the ordinary product lookup, so a slug that predates this rule keeps working.
    """
    return value.lower().startswith(RESERVED_SLUG_PREFIXES)


def normalise_slug(value: str) -> str:
    """Strip surrounding whitespace and validate the slug.

    Raises:
        InvalidSlugError: the value cannot be used in a Telegram deep link, or
            it is reserved for a non-product payload.
    """
    candidate = value.strip()
    if not is_valid_slug(candidate):
        message = (
            f"Slug must match {SLUG_PATTERN}: 1-{SLUG_MAX_LENGTH} characters "
            f"from A-Z, a-z, 0-9, underscore and hyphen"
        )
        raise InvalidSlugError(message, slug=value)
    if is_reserved_slug(candidate):
        reserved = ", ".join(RESERVED_SLUG_PREFIXES)
        message = f"Slug must not start with a reserved prefix ({reserved})"
        raise InvalidSlugError(message, slug=value)
    return candidate
