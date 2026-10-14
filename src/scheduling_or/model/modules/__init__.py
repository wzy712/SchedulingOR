"""Constraint module registry.

Importing this package registers every module. ``instantiate`` maps a spec
constraint (a pydantic discriminated union member) onto its module class, so
adding a constraint type is a two-line change: write the class, decorate it.
"""

from __future__ import annotations

from typing import Any

from . import hard as _hard  # noqa: F401  (import for registration side effect)
from . import soft as _soft  # noqa: F401
from .base import (
    HARD_REGISTRY,
    SOFT_REGISTRY,
    BuildContext,
    ConstraintModule,
    Ledger,
    Term,
    register_hard,
    register_soft,
)

__all__ = [
    "HARD_REGISTRY",
    "SOFT_REGISTRY",
    "BuildContext",
    "ConstraintModule",
    "Ledger",
    "Term",
    "instantiate",
    "known_types",
    "register_hard",
    "register_soft",
]


def instantiate(cfg: Any) -> ConstraintModule:
    """Build the module for a spec constraint. Raises on an unregistered type."""
    type_name = getattr(cfg, "type", None)
    if type_name is None:
        raise ValueError(f"constraint {cfg!r} has no `type` field")
    registry = HARD_REGISTRY if type_name in HARD_REGISTRY else SOFT_REGISTRY
    if type_name not in registry:
        raise ValueError(
            f"no module registered for constraint type {type_name!r}; "
            f"known types: {sorted([*HARD_REGISTRY, *SOFT_REGISTRY])}"
        )
    return registry[type_name](cfg)


def known_types() -> dict[str, list[str]]:
    return {"hard": sorted(HARD_REGISTRY), "soft": sorted(SOFT_REGISTRY)}
