"""Constraint module framework.

Every constraint is a small class that adds its own linear constraints to the
model and, for soft constraints, registers penalty terms in a shared ledger.

Why a ledger instead of each module calling ``model.minimize``: the objective must
be inspectable and reportable per constraint id ("this fairness goal cost you
100 out of 1830"). A module calling ``minimize`` directly would make that
impossible and would also break tiered/lexicographic solving.
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ortools.sat.python import cp_model

from ...schema.instance import Instance
from ...schema.spec import Spec
from ..vars import DecisionVars


@dataclass
class Term:
    """One additive contribution to the objective."""

    constraint_id: str
    family: str
    label: str
    var: cp_model.IntVar
    weight: int

    def violation(self, solver: cp_model.CpSolver) -> int:
        return int(solver.value(self.var))


@dataclass
class Ledger:
    """Ordered register of objective terms, keyed by constraint id."""

    terms: list[Term] = field(default_factory=list)

    def add(
        self,
        constraint_id: str,
        family: str,
        var: cp_model.IntVar,
        weight: int,
        label: str = "",
    ) -> Term:
        if weight < 0:
            raise ValueError("weights must be non-negative; minimise the violation, not the reward")
        t = Term(constraint_id, family, label, var, weight)
        self.terms.append(t)
        return t

    def ids(self) -> list[str]:
        seen: list[str] = []
        for t in self.terms:
            if t.constraint_id not in seen:
                seen.append(t.constraint_id)
        return seen

    def expression(self, *, only: set[str] | None = None) -> Any:
        chosen = [t for t in self.terms if only is None or t.constraint_id in only]
        if not chosen:
            return 0
        return sum(t.var * t.weight for t in chosen)

    def is_empty(self) -> bool:
        return not self.terms


@dataclass
class BuildContext:
    instance: Instance
    spec: Spec
    model: cp_model.CpModel
    vars: DecisionVars
    groups: dict[str, list[str]]
    ledger: Ledger = field(default_factory=Ledger)
    #: non-fatal modelling notes surfaced in the report (e.g. "constraint skipped")
    notes: list[str] = field(default_factory=list)

    def members(self, group_id: str | None) -> list[str]:
        if group_id is None:
            return list(self.vars.people)
        return list(self.groups[group_id])

    def note(self, message: str) -> None:
        self.notes.append(message)


class ConstraintModule(abc.ABC):
    """Base class for one constraint or one soft objective."""

    kind: ClassVar[str] = "hard"
    family: ClassVar[str] = ""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    @property
    def id(self) -> str:
        return str(getattr(self.cfg, "id", "") or self.family)

    @abc.abstractmethod
    def build(self, ctx: BuildContext) -> None:
        """Add constraints to ``ctx.model`` and/or terms to ``ctx.ledger``."""

    # -- shared helpers ---------------------------------------------------
    def transposed(
        self, ctx: BuildContext, dates: list[dt.date] | None = None
    ) -> list[tuple[dt.date, str]]:
        return [(d, s) for d in (dates or ctx.vars.dates) for s in ctx.vars.shifts]

    def all_sane_shifts(self, ctx: BuildContext) -> list[str]:
        return list(ctx.vars.shifts)


HARD_REGISTRY: dict[str, type[ConstraintModule]] = {}
SOFT_REGISTRY: dict[str, type[ConstraintModule]] = {}


def register_hard(type_name: str):
    def deco(cls: type[ConstraintModule]) -> type[ConstraintModule]:
        cls.kind = "hard"
        cls.family = type_name
        HARD_REGISTRY[type_name] = cls
        return cls

    return deco


def register_soft(type_name: str):
    def deco(cls: type[ConstraintModule]) -> type[ConstraintModule]:
        cls.kind = "soft"
        cls.family = type_name
        SOFT_REGISTRY[type_name] = cls
        return cls

    return deco
