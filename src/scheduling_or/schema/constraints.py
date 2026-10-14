"""Constraint schema: the *policy* layer, and the main product of the interview.

Hard and soft constraints are **different types**, not the same thing with a big
weight. That is deliberate: a violated hard constraint makes the roster
unpublishable (report INFEASIBLE and explain why); a violated soft constraint
costs something and shows up in the cost breakdown.

Each constraint carries a stable ``id``. Diagnostics and the cost report key off
it, so ids must be stable across runs.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .instance import Weekday

_STRICT = ConfigDict(extra="forbid")
Period = Literal["week", "horizon"]
Aggregation = Literal["spread", "deviation"]


class _Base(BaseModel):
    model_config = _STRICT

    id: str = Field(default="", description="stable identifier; auto-derived when empty")
    enabled: bool = True
    note: str = ""


# --------------------------------------------------------------------------
# HARD
# --------------------------------------------------------------------------
class OneShiftPerDay(_Base):
    """At most ``max_per_day`` real shifts per person per day."""

    type: Literal["one_shift_per_day"] = "one_shift_per_day"
    max_per_day: int = Field(default=1, ge=1)


class RestBetweenShifts(_Base):
    """Minimum rest between the end of one shift and the start of the next."""

    type: Literal["rest_between_shifts"] = "rest_between_shifts"
    min_hours: float = Field(default=0.0, ge=0, le=72)


class MaxConsecutiveWorkDays(_Base):
    type: Literal["max_consecutive_work_days"] = "max_consecutive_work_days"
    days: int = Field(ge=1)
    include_history: bool = Field(
        default=True, description="count shifts recorded in person.history before the horizon"
    )


class ForbidTransition(_Base):
    """Sequence ``**from** then **to** on consecutive days is illegal."""

    type: Literal["forbid_transition"] = "forbid_transition"
    from_shift: str = Field(alias="from")
    to_shift: str = Field(alias="to")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WeeklyRestDays(_Base):
    type: Literal["weekly_rest_days"] = "weekly_rest_days"
    min: int = Field(ge=1, le=7)


class MaxShiftsPerPeriod(_Base):
    type: Literal["max_shifts_per_period"] = "max_shifts_per_period"
    period: Period = "horizon"
    max: int = Field(ge=1)
    group: str | None = Field(default=None, description="apply only to this group")


class HoursBounds(_Base):
    type: Literal["hours_bounds"] = "hours_bounds"
    period: Period = "horizon"
    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, gt=0)
    group: str | None = None

    @model_validator(mode="after")
    def _bounds(self) -> HoursBounds:
        if self.min is None and self.max is None:
            raise ValueError("hours_bounds needs `min` and/or `max`")
        if self.min is not None and self.max is not None and self.max < self.min:
            raise ValueError(f"hours_bounds max {self.max} below min {self.min}")
        return self


class Requirement(_Base):
    """Qualification / coverage floor: at least ``min`` people from ``group`` on ``shift``.

    This is the primitive that expresses "the night shift needs an ICU-certified
    nurse" or "each exam room needs one subject-expert invigilator".
    """

    type: Literal["requirement"] = "requirement"
    shift: str
    group: str
    min: int = Field(default=1, ge=1)
    max: int | None = Field(default=None, ge=1)
    weekday: Weekday | None = None
    dates: list[dt.date] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_filter(self) -> Requirement:
        if self.weekday is not None and self.dates:
            raise ValueError("requirement takes at most one of `weekday` or `dates`")
        return self


class MinConsecutiveWorkDays(_Base):
    """Any working streak must last at least ``days`` days.

    Forbids fragmented single-day stints (one day on, one day off, repeat) -- a real
    complaint in duty rosters. The complement of ``max_consecutive_work_days``.
    """

    type: Literal["min_consecutive_work_days"] = "min_consecutive_work_days"
    days: int = Field(default=2, ge=2, le=14)


class FixedAssignment(_Base):
    """Pin a person to a shift on a date (or to rest when ``shift`` is the rest code)."""

    type: Literal["fixed_assignment"] = "fixed_assignment"
    person: str
    date: str = Field(description="'YYYY-MM-DD'")
    shift: str


class NoPair(_Base):
    """These people must not be assigned to the same shift on the same day."""

    type: Literal["no_pair"] = "no_pair"
    people: list[str] = Field(min_length=2)


class Pairing(_Base):
    """A junior may only work a shift when a senior from ``senior_group`` also works it."""

    type: Literal["pairing"] = "pairing"
    junior_group: str
    senior_group: str


HardConstraint = Annotated[
    OneShiftPerDay | RestBetweenShifts | MaxConsecutiveWorkDays | ForbidTransition | WeeklyRestDays | MaxShiftsPerPeriod | HoursBounds | Requirement | MinConsecutiveWorkDays | FixedAssignment | NoPair | Pairing,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# SOFT
# --------------------------------------------------------------------------
class CoverageShortfall(_Base):
    """Penalise under-staffing. Weight should dominate every other soft term."""

    type: Literal["coverage_shortfall"] = "coverage_shortfall"
    weight: int = Field(default=1000, ge=0)


class CoverageExcess(_Base):
    type: Literal["coverage_excess"] = "coverage_excess"
    weight: int = Field(default=20, ge=0)


class Fairness(_Base):
    """Balance a workload metric across people.

    ``metric`` is ``total_shifts`` or ``shift:<shift-id>``; ``aggregation`` is the
    max-min ``spread`` or the absolute ``deviation`` from the mean.
    """

    type: Literal["fairness"] = "fairness"
    metric: str = "total_shifts"
    aggregation: Aggregation = "spread"
    weight: int = Field(default=50, ge=0)
    group: str | None = None

    @model_validator(mode="after")
    def _metric(self) -> Fairness:
        allowed = {"total_shifts", "weekend_shifts", "holiday_shifts"}
        if self.metric not in allowed and not self.metric.startswith("shift:"):
            raise ValueError(
                f"fairness metric must be one of {sorted(allowed)} or 'shift:<id>', "
                f"got {self.metric!r}"
            )
        return self


class Preference(_Base):
    """Reward/penalise requested or avoided assignments (see Instance.preferences)."""

    type: Literal["preference"] = "preference"
    weight: int = Field(default=30, ge=0)


class SkillShortfall(_Base):
    """Soft counterpart of ``requirement``: pay a price for missing qualified staff.

    Use when "no ICU-certified nurse on nights" should degrade the solution and show
    up in the cost report, instead of making the whole roster infeasible.
    """

    type: Literal["skill_shortfall"] = "skill_shortfall"
    shift: str
    group: str
    min: int = Field(default=1, ge=1)
    weight: int = Field(default=800, ge=0)


class ConsecutiveWorkPreference(_Base):
    """Softly prefer working streaks no longer than ``preferred_max`` days."""

    type: Literal["consecutive_work_preference"] = "consecutive_work_preference"
    preferred_max: int = Field(ge=1)
    weight: int = Field(default=10, ge=0)


class PenalizedTransition(_Base):
    """Discourage (but allow) a shift sequence such as afternoon -> night."""

    type: Literal["penalized_transition"] = "penalized_transition"
    from_shift: str = Field(alias="from")
    to_shift: str = Field(alias="to")
    weight: int = Field(default=10, ge=0)
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TargetShiftsDeviation(_Base):
    """Pull each person's shift count toward ``person.target_shifts`` / ``default_target``."""

    type: Literal["target_shifts_deviation"] = "target_shifts_deviation"
    default_target: int | None = Field(default=None, ge=0)
    weight: int = Field(default=30, ge=0)
    group: str | None = None


SoftConstraint = Annotated[
    CoverageShortfall | CoverageExcess | SkillShortfall | Fairness | Preference | ConsecutiveWorkPreference | PenalizedTransition | TargetShiftsDeviation,
    Field(discriminator="type"),
]


class ConstraintSet(BaseModel):
    model_config = _STRICT

    hard: list[HardConstraint] = Field(default_factory=list)
    soft: list[SoftConstraint] = Field(default_factory=list)

    def all_ids(self) -> list[str]:
        return [c.id for c in [*self.hard, *self.soft] if c.id]
