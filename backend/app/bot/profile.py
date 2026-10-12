"""The Telegram profile snapshot every entry point records.

Opening a product card, opening an invitation link and opening the bonus section
all remember who the visitor is, in exactly the same shape. Keeping that in one
function means the three entry points cannot drift apart in what they store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.commands import UserDraft

if TYPE_CHECKING:
    from aiogram.types import User


def profile_of(user: User) -> UserDraft:
    """Snapshot of the Telegram profile behind this update."""
    return UserDraft(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        language_code=user.language_code,
    )


__all__ = ["profile_of"]
