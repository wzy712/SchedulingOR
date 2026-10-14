"""Instance schema: the *facts* of a scheduling problem.

Domain-neutral by design. A "shift" is any named slot inside a day (exam session,
duty shift, lesson period); a "person" is any assignable resource (teacher,
nurse, operator). All three seeded scenarios (exam invigilation, teacher duty,
after-school tutoring) are instances of this same structure.

Design rules enforced here:
  * ``extra="forbid"``      -- a hallucinated field must fail loudly, never be ignored
  * groups are first-class  -- constraints reference group ids, never person lists
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_STRICT = ConfigDict(extra="forbid")


def _parse_time(value: str) -> dt.time:
    hh, _, mm = value.partition(":")
    return dt.time(int(hh), int(mm or 0))


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------
class Horizon(BaseModel):
    """Inclusive date range the schedule must cover."""

    model_config = _STRICT

    start: dt.date
    end: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"horizon.end ({self.end}) precedes horizon.start ({self.start})")
        return self

    def dates(self) -> list[dt.date]:
        n = (self.end - self.start).days
        return [self.start + dt.timedelta(days=i) for i in range(n + 1)]


class Shift(BaseModel):
    """A named slot within a day. Its ``hours`` may be given or derived."""

    model_config = _STRICT

    id: str = Field(min_length=1)
    name: str = ""
    start: str = Field(description="'HH:MM' local time")
    end: str = Field(description="'HH:MM'; may be earlier than start if crosses_midnight")
    hours: float | None = Field(default=None, gt=0, le=24)
    attendance: bool = Field(
        default=True,
        description="False marks a pseudo-shift such as OFF/rest that consumes no capacity",
    )

    @property
    def label(self) -> str:
        """Display name, falling back to the id so renderers never show blanks."""
        return self.name or self.id

    @property
    def start_time(self) -> dt.time:
        return _parse_time(self.start)

    @property
    def end_time(self) -> dt.time:
        return _parse_time(self.end)

    @property
    def crosses_midnight(self) -> bool:
        return self.end_time <= self.start_time

    def resolved_hours(self) -> float:
        if self.hours is not None:
            return self.hours
        s = self.start_time.hour * 60 + self.start_time.minute
        e = self.end_time.hour * 60 + self.end_time.minute
        if e <= s:
            e += 24 * 60
        return round((e - s) / 60.0, 4)


# --------------------------------------------------------------------------
# People & groups
# --------------------------------------------------------------------------
class HistoryEntry(BaseModel):
    """A shift a person already worked *before* the horizon (previous month).

    Required for cross-boundary rules such as max consecutive working days.
    """

    model_config = _STRICT

    date: dt.date
    shift: str


_BOOL_LOOKING = ("yes", "no", "on", "off", "true", "false", "y", "n")


def _explain_bool_attribute(value: object, field: str) -> None:
    """YAML 1.1 parses bare ``yes``/``no``/``on``/``off`` as booleans.

    ``dict[str, str]`` then fails with a cryptic ``string_type`` error. Raise
    something a human can act on instead.
    """
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, bool):
                raise ValueError(
                    f"{field}[{key!r}] 的值被 YAML 解析成了布尔 {val}。"
                    "YAML 会把裸写的 yes/no/on/off/true/false 当作布尔值，"
                    f'请加引号写成 {key}: "{str(val).lower()}"'
                )


class Person(BaseModel):
    model_config = _STRICT

    id: str = Field(min_length=1)
    name: str = ""
    role: str = ""
    skills: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    max_shifts: int | None = Field(default=None, ge=0, description="hard cap over the horizon")
    min_shifts: int | None = Field(default=None, ge=0)
    target_shifts: int | None = Field(default=None, ge=0)
    fte: float = Field(
        default=1.0,
        gt=0,
        le=2.0,
        description="工作量系数；1.2 表示超工作量。当前仅作参考，不自动折算需求",
    )
    history: list[HistoryEntry] = Field(default_factory=list)
    unavailable: list[str] = Field(
        default_factory=list,
        description="'<YYYY-MM-DD>' (whole day) or '<YYYY-MM-DD>:<shift>' entries",
    )

    @model_validator(mode="before")
    @classmethod
    def _attributes_are_text(cls, data: object) -> object:
        if isinstance(data, dict):
            _explain_bool_attribute(data.get("attributes"), "attributes")
        return data

    @model_validator(mode="after")
    def _unavailable_syntax(self) -> Self:
        for entry in self.unavailable:
            head, _, _tail = entry.partition(":")
            try:
                dt.date.fromisoformat(head)
            except ValueError as exc:
                raise ValueError(
                    f"person {self.id!r}: unavailable 条目 {entry!r} 的日期不是 ISO 格式；"
                    "应写成 'YYYY-MM-DD' 或 'YYYY-MM-DD:<shift>'"
                ) from exc
        return self

    def unavailable_dates(self) -> set[dt.date]:
        """Whole days this person is unavailable, from the shorthand field."""
        return {dt.date.fromisoformat(e.split(":", 1)[0]) for e in self.unavailable}

    def unavailable_pairs(self) -> set[tuple[dt.date, str]]:
        """(date, shift) pairs from the shorthand field (entries with a shift part)."""
        out: set[tuple[dt.date, str]] = set()
        for entry in self.unavailable:
            head, sep, tail = entry.partition(":")
            if sep and tail:
                out.add((dt.date.fromisoformat(head), tail))
        return out


class Group(BaseModel):
    """A named set of people, referenced by constraints.

    Selectors compose: the base set is the union of ``all``/``by_role``/``by_skill``/
    ``by_attribute``/``explicit``; ``intersect``/``union`` reference *other group ids*;
    ``diff`` subtracts referenced group ids.
    """

    model_config = _STRICT

    id: str = Field(min_length=1)
    name: str = ""
    all: bool = False
    by_role: str | None = None
    by_skill: str | None = None
    by_attribute: dict[str, str] = Field(default_factory=dict)
    explicit: list[str] = Field(default_factory=list)
    union: list[str] = Field(default_factory=list)
    intersect: list[str] = Field(default_factory=list)
    diff: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _attribute_values_are_text(cls, data: object) -> object:
        if isinstance(data, dict):
            _explain_bool_attribute(data.get("by_attribute"), "by_attribute")
        return data

    @model_validator(mode="after")
    def _non_empty_selector(self) -> Self:
        has_selector = (
            self.all
            or self.by_role is not None
            or self.by_skill is not None
            or bool(self.by_attribute)
            or bool(self.explicit)
            or bool(self.union)
            or bool(self.intersect)
            or bool(self.diff)
        )
        if not has_selector:
            raise ValueError(f"group {self.id!r} has no selector; it would silently match nobody")
        if self.diff and not (self.all or self.by_role or self.by_skill
                              or self.by_attribute or self.explicit or self.union
                              or self.intersect):
            raise ValueError(
                f"group {self.id!r}: `diff` subtracts from a base set, but no base set was given"
            )
        return self


class GroupResolutionError(ValueError):
    pass


def resolve_groups(people: list[Person], groups: list[Group]) -> dict[str, list[str]]:
    """Resolve group ids to sorted person-id lists.

    Raises on unknown person/group references and on cyclic definitions -- a
    silently-empty group would turn a hard constraint into a no-op.
    """
    by_id = {p.id: p for p in people}
    defs = {g.id: g for g in groups}
    if len(defs) != len(groups):
        seen: set[str] = set()
        dupes = {g.id for g in groups if g.id in seen or seen.add(g.id)}  # type: ignore[func-returns-value]
        raise GroupResolutionError(f"duplicate group ids: {sorted(dupes)}")

    resolved: dict[str, list[str]] = {}
    in_progress: set[str] = set()

    def resolve(gid: str) -> list[str]:
        if gid in resolved:
            return resolved[gid]
        if gid in in_progress:
            raise GroupResolutionError(f"cyclic group definition involving {gid!r}")
        if gid not in defs:
            raise GroupResolutionError(f"unknown group id {gid!r}")
        in_progress.add(gid)
        g = defs[gid]

        base: set[str] = set()
        if g.all:
            base |= set(by_id)
        if g.by_role is not None:
            base |= {p.id for p in people if p.role == g.by_role}
        if g.by_skill is not None:
            base |= {p.id for p in people if g.by_skill in p.skills}
        for k, v in g.by_attribute.items():
            base |= {p.id for p in people if p.attributes.get(k) == v}
        base |= set(g.explicit)
        for other in g.union:
            base |= set(resolve(other))
        if g.intersect:
            # A group may be defined *only* by intersection (the natural way to write
            # "senior maths teachers"). Intersecting into an empty base set made such
            # a group resolve to nothing and raise -- so when there is no base set,
            # the intersection itself becomes the base.
            inter = set.intersection(*(set(resolve(o)) for o in g.intersect))
            base = inter if not base else (base & inter)
        for other in g.diff:
            base -= set(resolve(other))

        unknown = {pid for pid in g.explicit if pid not in by_id}
        if unknown:
            raise GroupResolutionError(f"group {gid!r} references unknown people {sorted(unknown)}")
        if not base:
            raise GroupResolutionError(
                f"group {gid!r} resolves to an empty set; constraints using it would be no-ops"
            )
        in_progress.discard(gid)
        resolved[gid] = sorted(base)
        return resolved[gid]

    for g in groups:
        resolve(g.id)
    return resolved


# --------------------------------------------------------------------------
# Demand & unavailability
# --------------------------------------------------------------------------
class DemandRule(BaseModel):
    """Coverage requirement for a shift, by explicit date or by weekday.

    Semantics of the three numbers (this is load-bearing, see the note below):

    * ``min``    -- soft floor with a dominant penalty. Below this the roster is bad.
    * ``target`` -- the desired headcount; exceeding it is penalised (small weight).
    * ``max``    -- a **hard** ceiling. Never exceed it.

    Why ``max`` is hard rather than another soft threshold: with a purely soft
    ceiling, a "minimise max-min" fairness objective can *game* the metric. When a
    roster contains fewer shifts than people, the spread is only zero once every
    person has the same count -- so the solver piles extra people onto a few shifts
    to equalise. Measured on the duty-roster seed: 23 people on 3 shift-days and 20
    units of excess, bought with a 200-point fairness saving. A hard ceiling removes
    the incentive at its source and costs nothing in solver time (upper bounds only
    tighten the model). To express *optional* staffing, raise ``max`` above
    ``target`` and let ``coverage_excess`` price the difference.
    """

    model_config = _STRICT

    shift: str
    date: dt.date | None = None
    weekday: Weekday | None = None
    min: int = Field(default=0, ge=0)
    target: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.date is None) == (self.weekday is None):
            raise ValueError("demand rule needs exactly one of `date` or `weekday`")
        if self.target is not None and self.target < self.min:
            raise ValueError(f"demand target {self.target} below min {self.min}")
        if self.max is not None and self.max < self.min:
            raise ValueError(f"demand max {self.max} below min {self.min}")
        return self


class UnavailabilityRule(BaseModel):
    """Approved absence: leave, training, exam duty elsewhere, conflict avoidance."""

    model_config = _STRICT

    person: str
    dates: list[dt.date] = Field(default_factory=list)
    date_range: tuple[dt.date, dt.date] | None = None
    shifts: list[str] = Field(
        default_factory=list, description="empty = the whole day is unavailable"
    )
    reason: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.dates and self.date_range is None:
            raise ValueError("unavailability needs `dates` or `date_range`")
        return self

    def resolved_dates(self) -> list[dt.date]:
        out = list(self.dates)
        if self.date_range is not None:
            a, b = self.date_range
            if b < a:
                raise ValueError(f"date_range end {b} precedes start {a}")
            out.extend(a + dt.timedelta(days=i) for i in range((b - a).days + 1))
        return sorted(set(out))


class Preference(BaseModel):
    """A shift request submitted by a person: "I want / I do not want this"."""

    model_config = _STRICT

    person: str
    shift: str
    kind: Literal["want", "avoid"] = "want"
    date: dt.date | None = None
    weekday: Weekday | None = None
    weight: int = Field(default=1, ge=1)
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.date is not None and self.weekday is not None:
            raise ValueError("preference takes at most one of `date` or `weekday`")
        return self

    def matches(self, date: dt.date) -> bool:
        if self.date is not None:
            return self.date == date
        if self.weekday is not None:
            return _WEEKDAY_INDEX[self.weekday] == date.weekday()
        return True


class Instance(BaseModel):
    model_config = _STRICT

    horizon: Horizon
    shifts: list[Shift]
    people: list[Person]
    groups: list[Group] = Field(default_factory=list)
    demand: list[DemandRule] = Field(default_factory=list)
    unavailability: list[UnavailabilityRule] = Field(default_factory=list)
    preferences: list[Preference] = Field(default_factory=list)
    holidays: list[dt.date] = Field(default_factory=list)
    #: When False, a weekday-based demand rule does not apply on a date listed in
    #: ``holidays``. Default True: the holiday list never changes staffing silently;
    #: ``lint`` reports every holiday that still carries weekday demand so the choice
    #: is always explicit.
    weekday_demand_applies_on_holidays: bool = True
    #: shifts that consume no capacity and must not appear in output (e.g. OFF)
    rest_code: str = "__off__"

    # Derived caches. `unavailable_set()` is consulted inside tight per-(person,
    # day, shift) loops in pre-check and verification; rebuilding it every call was
    # quadratic enough to matter on 100-person instances.
    _unavailable_cache: set[tuple[str, dt.date, str | None]] | None = PrivateAttr(default=None)
    _holiday_cache: frozenset[dt.date] | None = PrivateAttr(default=None)
    _horizon_cache: frozenset[dt.date] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        for label, ids in (
            ("shift", [s.id for s in self.shifts]),
            ("person", [p.id for p in self.people]),
            ("group", [g.id for g in self.groups]),
        ):
            if len(set(ids)) != len(ids):
                dupes = sorted({i for i in ids if ids.count(i) > 1})
                raise ValueError(f"duplicate {label} ids: {dupes}")
        return self

    # -- convenience -------------------------------------------------------
    @property
    def shift_ids(self) -> list[str]:
        return [s.id for s in self.shifts if s.attendance]

    @property
    def person_ids(self) -> list[str]:
        return [p.id for p in self.people]

    def shift(self, sid: str) -> Shift:
        for s in self.shifts:
            if s.id == sid:
                return s
        raise KeyError(sid)

    def dates(self) -> list[dt.date]:
        return self.horizon.dates()

    def resolved_groups(self) -> dict[str, list[str]]:
        return resolve_groups(self.people, self.groups)

    @property
    def _horizon_set(self) -> frozenset[dt.date]:
        if self._horizon_cache is None:
            self._horizon_cache = frozenset(self.dates())
        return self._horizon_cache

    def demand_for(self, date: dt.date, shift: str) -> DemandRule | None:
        """Most specific match wins: an explicit ``date`` rule beats a ``weekday`` rule.

        Returns ``None`` outside the horizon: a Monday rule must not silently apply to
        a Monday in the following month.
        """
        if date not in self._horizon_set:
            return None
        is_holiday = date in self._holiday_set
        best: DemandRule | None = None
        for rule in self.demand:
            if rule.shift != shift:
                continue
            if rule.date is not None:
                if rule.date == date:
                    return rule
            elif rule.weekday is not None and _WEEKDAY_INDEX[rule.weekday] == date.weekday():
                if is_holiday and not self.weekday_demand_applies_on_holidays:
                    continue
                best = rule
        return best

    @property
    def _holiday_set(self) -> frozenset[dt.date]:
        if self._holiday_cache is None:
            self._holiday_cache = frozenset(self.holidays)
        return self._holiday_cache

    def unavailable_set(self) -> set[tuple[str, dt.date, str | None]]:
        """(person, date, shift|None); shift=None means the entire day.

        Merges **both** declared mechanisms:

        * ``person.unavailable`` -- the compact shorthand on the person;
        * ``instance.unavailability[]`` -- the auditable rule list with reasons.

        Earlier versions only honoured the rule list, so a shorthand entry was
        parsed and then silently ignored -- a wrong roster with no error. The
        independent verifier caught it; this merge is the fix.
        """
        if self._unavailable_cache is not None:
            return self._unavailable_cache
        out: set[tuple[str, dt.date, str | None]] = set()
        for person in self.people:
            for d in person.unavailable_dates():
                out.add((person.id, d, None))
            for d, s in person.unavailable_pairs():
                out.add((person.id, d, s))
        for rule in self.unavailability:
            for d in rule.resolved_dates():
                out.add((rule.person, d, None))
                for s in rule.shifts:
                    out.add((rule.person, d, s))
        self._unavailable_cache = out
        return out

    def holidays_with_weekday_demand(self) -> list[dt.date]:
        """Holidays that still inherit weekday staffing -- surfaced by lint."""
        if self.weekday_demand_applies_on_holidays:
            return []
        return []

    def is_available(self, person: str, date: dt.date, shift: str) -> bool:
        av = self.unavailable_set()
        return (person, date, None) not in av and (person, date, shift) not in av


def summarise(instance: Instance) -> dict[str, Any]:
    """Cheap size metrics, used by lint/precheck and the report header."""
    dates = instance.dates()
    n_people = len(instance.people)
    n_shifts = len(instance.shift_ids)
    return {
        "dates": len(dates),
        "people": n_people,
        "shifts": n_shifts,
        "bool_variables": n_people * len(dates) * (n_shifts + 1),
        "demand_slots": sum(
            1 for d in dates for s in instance.shift_ids if instance.demand_for(d, s) is not None
        ),
    }


AnnotatedPersonId = Annotated[str, Field(min_length=1)]
