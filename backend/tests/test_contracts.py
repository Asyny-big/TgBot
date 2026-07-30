"""The concrete repositories must satisfy the domain contracts.

Assigning each implementation to its ``Protocol`` type makes mypy verify the
whole signature — argument names, keyword-only markers and return types — so a
drift between contract and implementation fails the type check, not production.
The runtime assertions below guard against a method disappearing entirely.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from app.domain.repositories import (
    ProductRepository,
    PurchaseRepository,
    StatsRepository,
    UserRepository,
)

if TYPE_CHECKING:
    from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
    from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
    from app.infrastructure.db.repositories.stats import SqlAlchemyStatsRepository
    from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository


def _protocol_methods(protocol: type) -> set[str]:
    return {
        name
        for name in vars(protocol)
        if not name.startswith("_") and callable(getattr(protocol, name, None))
    }


def test_product_repository_satisfies_the_contract(
    products: SqlAlchemyProductRepository,
) -> None:
    contract: ProductRepository = products
    assert _protocol_methods(ProductRepository) <= set(dir(contract))


def test_user_repository_satisfies_the_contract(users: SqlAlchemyUserRepository) -> None:
    contract: UserRepository = users
    assert _protocol_methods(UserRepository) <= set(dir(contract))


def test_purchase_repository_satisfies_the_contract(
    purchases: SqlAlchemyPurchaseRepository,
) -> None:
    contract: PurchaseRepository = purchases
    assert _protocol_methods(PurchaseRepository) <= set(dir(contract))


def test_stats_repository_satisfies_the_contract(stats: SqlAlchemyStatsRepository) -> None:
    contract: StatsRepository = stats
    assert _protocol_methods(StatsRepository) <= set(dir(contract))


def test_contracts_are_fully_annotated() -> None:
    """No repository method may hide an untyped argument or return value.

    Annotations are read as written (not resolved): the contract module keeps its
    entity imports inside ``TYPE_CHECKING``, which is exactly the point.
    """
    for protocol in (ProductRepository, UserRepository, PurchaseRepository, StatsRepository):
        for name in _protocol_methods(protocol):
            method = getattr(protocol, name)
            annotations = method.__annotations__
            signature = inspect.signature(method)
            assert "return" in annotations, f"{protocol.__name__}.{name} lacks a return type"
            for parameter in signature.parameters.values():
                if parameter.name == "self":
                    continue
                assert parameter.name in annotations, (
                    f"{protocol.__name__}.{name}({parameter.name}) is not annotated"
                )
