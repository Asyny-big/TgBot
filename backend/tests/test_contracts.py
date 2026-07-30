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
    # Only referenced from annotations: the conformance helpers below exist for
    # mypy, not for the interpreter.
    from app.domain.auth import TokenRevocationStore
    from app.domain.delivery import DeliveryGateway
    from app.domain.locks import LockManager
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.cache.revocation import RedisTokenRevocationStore
    from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
    from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
    from app.infrastructure.db.repositories.stats import SqlAlchemyStatsRepository
    from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWork, SqlAlchemyUnitOfWorkFactory
    from tests.fakes import FakeDeliveryGateway, FakeLockManager, FakeUnitOfWorkFactory


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


# --------------------------------------------------------------------------- #
# Static conformance: these functions never run, mypy checks them.
# --------------------------------------------------------------------------- #
def _lock_manager_conforms(manager: RedisLockManager) -> LockManager:
    return manager


def _fake_lock_manager_conforms(manager: FakeLockManager) -> LockManager:
    return manager


def _unit_of_work_conforms(unit: SqlAlchemyUnitOfWork) -> UnitOfWork:
    return unit


def _unit_of_work_factory_conforms(
    factory: SqlAlchemyUnitOfWorkFactory,
) -> UnitOfWorkFactory:
    return factory


def _fake_unit_of_work_factory_conforms(factory: FakeUnitOfWorkFactory) -> UnitOfWorkFactory:
    return factory


def _delivery_gateway_conforms(gateway: FakeDeliveryGateway) -> DeliveryGateway:
    return gateway


def _revocation_store_conforms(store: RedisTokenRevocationStore) -> TokenRevocationStore:
    return store


def test_the_contract_helpers_are_importable() -> None:
    """Keeps the conformance helpers from being flagged as dead code."""
    assert _lock_manager_conforms is not None
    assert _unit_of_work_factory_conforms is not None
    assert _delivery_gateway_conforms is not None
