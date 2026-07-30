"""Translation of database integrity errors into domain errors.

A unique violation is a business outcome ("this slug is taken", "this buyer
already owns the product"), not an infrastructure crash — so it is translated at
the boundary instead of leaking SQLAlchemy exceptions upwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.exc import IntegrityError


def violated_constraint(error: IntegrityError) -> str | None:
    """Return the name of the constraint that rejected the statement."""
    candidates = (error.orig, getattr(error.orig, "__cause__", None))
    for candidate in candidates:
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
    return None


def like_pattern(term: str) -> str:
    """Escape LIKE wildcards so user input cannot alter the search semantics."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


LIKE_ESCAPE = "\\"
