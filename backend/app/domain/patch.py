"""Partial update primitives.

A partial update must distinguish "field not provided" from "field explicitly
set to null" — clearing a product photo is not the same as leaving it alone.
``UNSET`` is that distinction, and it is type safe.
"""

from __future__ import annotations

from enum import Enum
from typing import TypeGuard


class Unset(Enum):
    """Single-member sentinel enum: the value was not supplied."""

    SENTINEL = "unset"

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNSET"


UNSET = Unset.SENTINEL

type Maybe[ValueT] = ValueT | Unset
"""Either a concrete value or the ``UNSET`` sentinel."""


def is_set[ValueT](value: Maybe[ValueT]) -> TypeGuard[ValueT]:
    """Narrow a ``Maybe`` to its value type when it was supplied."""
    return not isinstance(value, Unset)
