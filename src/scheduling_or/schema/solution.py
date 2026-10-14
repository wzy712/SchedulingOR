"""Solution schema: what the solver produced, and how much each soft goal cost.

Everything here must be JSON-serialisable. A CP-SAT expression object leaking
into this layer is a bug (measured: ``SumArray`` breaks ``json.dumps``), so
values are materialised with ``solver.value(...)`` before construction.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")

SolveStatus = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"]


class Assignment(BaseModel):
    model_config = _STRICT

    person: str
    date: dt.date
    shift: str


class ObjectiveTerm(BaseModel):
    """One soft-constraint family's contribution to the objective."""

    model_config = _STRICT

    constraint_id: str
    family: str
    label: str = ""
    violations: int = 0
    weight: int = 0
    cost: int = 0
    #: fraction of the total objective, filled in by the report layer
    share: float = 0.0


class FairnessMetric(BaseModel):
    model_config = _STRICT

    metric: str
    per_person: dict[str, int] = Field(default_factory=dict)
    minimum: int = 0
    maximum: int = 0
    mean: float = 0.0
    spread: int = 0
    stddev: float = 0.0
    gini: float = 0.0


class PersonStats(BaseModel):
    model_config = _STRICT

    shifts: int = 0
    hours: float = 0.0
    by_shift: dict[str, int] = Field(default_factory=dict)
    weekend_shifts: int = 0
    preferences_requested: int = 0
    preferences_met: int = 0

    @property
    def preference_rate(self) -> float | None:
        if self.preferences_requested == 0:
            return None
        return round(self.preferences_met / self.preferences_requested, 4)


class CoverageRow(BaseModel):
    model_config = _STRICT

    date: dt.date
    shift: str
    minimum: int = 0
    target: int | None = None
    maximum: int | None = None
    assigned: int = 0

    @property
    def shortfall(self) -> int:
        return max(0, self.minimum - self.assigned)

    @property
    def excess(self) -> int:
        if self.maximum is not None:
            return max(0, self.assigned - self.maximum)
        if self.target is not None:
            return max(0, self.assigned - self.target)
        return 0


class Solution(BaseModel):
    model_config = _STRICT

    status: SolveStatus
    feasible: bool
    #: **Total weighted cost** -- the sum of every soft term. This is what is
    #: comparable across runs and strategies. (It used to be
    #: ``solver.objective_value``, which in lexicographic mode is only the *last
    #: tier's* value: the report showed "objective 140" while the cost table summed to
    #: 460. An agent spotted that inconsistency in its own reasoning.)
    objective: float | None = None
    #: Only meaningful for ``weighted_sum`` (or a single tier): a bound on the *total*
    #: objective. In multi-tier lexicographic mode the solver's bound belongs to the
    #: last tier, so reporting it as a bound on the total would be a lie.
    best_bound: float | None = None
    #: which strategy produced this solution
    strategy: str = ""
    #: per-tier objective values, in the order they were optimised and locked
    tier_objectives: list[float] = Field(default_factory=list)
    wall_time_s: float = 0.0
    num_booleans: int = 0
    num_conflicts: int = 0
    branches: int = 0
    assignments: list[Assignment] = Field(default_factory=list)
    rest_code: str = "__off__"
    cost_breakdown: list[ObjectiveTerm] = Field(default_factory=list)
    fairness: list[FairnessMetric] = Field(default_factory=list)
    per_person: dict[str, PersonStats] = Field(default_factory=dict)
    coverage: list[CoverageRow] = Field(default_factory=list)
    #: independent verification (never trust the solver's own report)
    verified: bool = False
    verification_errors: list[str] = Field(default_factory=list)
    #: populated when status == INFEASIBLE
    diagnosis: str | None = None
    conflicts: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def total_cost(self) -> int:
        return sum(t.cost for t in self.cost_breakdown)

    def roster(self) -> dict[str, dict[dt.date, str]]:
        """person -> date -> shift, containing **only the working days**.

        Days a person does not work are simply absent: a ``Solution`` does not carry
        the horizon, so it cannot enumerate rest days. Callers that need a dense grid
        should iterate ``Instance.dates()`` and treat a missing entry as
        ``rest_code`` -- which is exactly what the Excel exporter does.
        """
        out: dict[str, dict[dt.date, str]] = {}
        for a in self.assignments:
            out.setdefault(a.person, {})[a.date] = a.shift
        return out
