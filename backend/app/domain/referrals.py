"""Referral links and the rules that govern a referral relationship.

A referral link is *not* a product link. It carries no product, opens no card
and sells nothing — it only records "B was invited by A". Afterwards B opens
ordinary product deep links, and the relationship keeps applying:

    A invites B
      day 1 → /start ohhh
      day 3 → /start muse
      day 7 → /start xoxo

Eligibility is therefore evaluated when a purchase is created, never when the
link is opened.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

REFERRAL_PAYLOAD_PREFIX: Final = "ref_"
"""Deep-link payload prefix that marks an invitation rather than a product."""

REFERRAL_CODE_LENGTH: Final = 10
REFERRAL_CODE_MAX_LENGTH: Final = 16
"""Stored column width. Longer than the generated code, so the code can grow."""

_CODE_ALPHABET: Final = "abcdefghjkmnpqrstuvwxyz23456789"
"""Lower-case base31 without ``i``, ``l``, ``o``, ``0`` and ``1``.

A referral code is read off a screen and typed by humans, so the characters
that are routinely confused for one another are simply not in the alphabet.
31 symbols over 10 positions is about 49 bits — not guessable by brute force
through a rate limited bot.
"""


class ReferralOutcome(StrEnum):
    """What happened when an invitation link was opened.

    Every outcome is a normal, expected result: opening someone's link twice or
    opening your own is a user mistake, not a system failure.
    """

    LINKED = "linked"
    """A new relationship was recorded and the discount is now available."""

    ALREADY_LINKED = "already_linked"
    """This buyer already has a referrer; it is never overwritten."""

    SELF_REFERRAL = "self_referral"
    """The inviter and the invited are the same Telegram account."""

    NOT_ELIGIBLE = "not_eligible"
    """The buyer already has purchase history, so no relationship is created."""

    UNKNOWN_CODE = "unknown_code"
    """The code belongs to nobody — a stale or mistyped link."""

    DISABLED = "disabled"
    """The referral programme is switched off by configuration."""

    @property
    def is_linked(self) -> bool:
        """Whether the buyer may now expect the first-purchase discount."""
        return self is ReferralOutcome.LINKED


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferralRegistration:
    """Result of opening an invitation link."""

    outcome: ReferralOutcome
    referrer_user_id: int | None = None

    @property
    def is_linked(self) -> bool:
        """Whether a fresh relationship was created by this visit."""
        return self.outcome.is_linked


def generate_referral_code() -> str:
    """Return a fresh, unpredictable referral code.

    Built from ``secrets`` rather than the Telegram id: the code travels in a
    public link, so it must not leak an account identifier, and it must not be
    derivable from a username either.
    """
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))


def referral_payload(code: str) -> str:
    """Deep-link payload that records an invitation from this code's owner."""
    return f"{REFERRAL_PAYLOAD_PREFIX}{code}"


def is_referral_payload(payload: str) -> bool:
    """Whether a ``/start`` payload looks like an invitation link."""
    return payload.startswith(REFERRAL_PAYLOAD_PREFIX)


def parse_referral_payload(payload: str) -> str | None:
    """Extract the referral code from a payload, or ``None`` if it is not one.

    Returning ``None`` for a malformed code (rather than raising) is what lets
    the ``/start`` handler fall through to the ordinary product lookup: a
    product whose slug happens to begin with ``ref_`` keeps working.
    """
    if not is_referral_payload(payload):
        return None
    code = payload[len(REFERRAL_PAYLOAD_PREFIX) :]
    if not code or len(code) > REFERRAL_CODE_MAX_LENGTH:
        return None
    if not all(character in _CODE_ALPHABET for character in code):
        return None
    return code


__all__ = [
    "REFERRAL_CODE_LENGTH",
    "REFERRAL_CODE_MAX_LENGTH",
    "REFERRAL_PAYLOAD_PREFIX",
    "ReferralOutcome",
    "ReferralRegistration",
    "generate_referral_code",
    "is_referral_payload",
    "parse_referral_payload",
    "referral_payload",
]
