"""Hard constraints: violating any of these makes the roster unpublishable.

They are added as real constraints (never as a huge weight). That is what lets the
kernel answer "which requirements conflict?" instead of "infeasible, good luck".
"""

from __future__ import annotations

import datetime as dt

from ortools.sat.python import cp_model

from ...schema.instance import _WEEKDAY_INDEX, Instance, Shift
from ..vars import history_works, period_chunks, prorated_rest
from .base import BuildContext, ConstraintModule, register_hard


def _abs_span(date: dt.date, shift: Shift) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(date, shift.start_time)
    end = dt.datetime.combine(date, shift.end_time)
    if shift.crosses_midnight or end <= start:
        end += dt.timedelta(days=1)
    return start, end


@register_hard("one_shift_per_day")
class OneShiftPerDay(ConstraintModule):
    """At most ``max_per_day`` real shifts per person per day.

    Note the model has no explicit OFF variable: "not working" is simply all real
    shifts being 0. That keeps the boolean count down (measured: 4500 vs 3600 for a
    30x30x4 instance) without losing expressiveness, because every rule we need is
    stated over ``works[p, d]``.
    """

    def build(self, ctx: BuildContext) -> None:
        cap = int(getattr(self.cfg, "max_per_day", 1))
        for p, d in ctx.vars.iter_person_days():
            real = ctx.vars.day(p, d)
            if not real:
                continue
            if cap == 1:
                ctx.model.add_at_most_one(real)
            else:
                ctx.model.add(sum(real) <= cap)


@register_hard("rest_between_shifts")
class RestBetweenShifts(ConstraintModule):
    """Forbid any two shifts whose gap is below the required rest.

    Checks same-day pairs and pairs up to ``min_hours/24 + 2`` days apart, so a
    24 h or 36 h rest requirement is honoured across day boundaries rather than
    only between adjacent days.
    """

    def build(self, ctx: BuildContext) -> None:
        min_hours = float(getattr(self.cfg, "min_hours", 0.0))
        if min_hours <= 0:
            return
        inst: Instance = ctx.instance
        shifts: list[Shift] = [inst.shift(s) for s in ctx.vars.shifts]
        max_span = int(min_hours // 24) + 2
        dates = ctx.vars.dates
        for p in ctx.vars.people:
            for i, d1 in enumerate(dates):
                for j in range(i, min(i + max_span, len(dates))):
                    d2 = dates[j]
                    for s1 in shifts:
                        _, end1 = _abs_span(d1, s1)
                        for s2 in shifts:
                            if s1.id == s2.id and d1 == d2:
                                continue
                            if d1 == d2 and (s2.start_time, s2.id) <= (s1.start_time, s1.id):
                                # same day: only the chronologically later shift can
                                # violate the rest after the earlier one. Checking the
                                # reverse order produced a negative "gap" and forbade
                                # perfectly valid combinations such as 09-11 + 20-22.
                                continue
                            start2, _ = _abs_span(d2, s2)
                            gap = (start2 - end1).total_seconds() / 3600.0
                            if gap < min_hours - 1e-9:
                                ctx.model.add_bool_or(
                                    [
                                        ~ctx.vars.on(p, d1, s1.id),
                                        ~ctx.vars.on(p, d2, s2.id),
                                    ]
                                )


@register_hard("max_consecutive_work_days")
class MaxConsecutiveWorkDays(ConstraintModule):
    """Sliding window over ``works``, optionally extended into pre-horizon history.

    The history extension is what makes month-boundary scheduling correct: without
    it a streak that began last month restarts at day 1 and the rule silently
    under-counts. Windows that fall entirely inside history are answered by the
    pre-check, not by the solver.
    """

    def build(self, ctx: BuildContext) -> None:
        cap = int(getattr(self.cfg, "days", 6))
        lookback = cap if bool(getattr(self.cfg, "include_history", True)) else 0
        for p in ctx.vars.people:
            head = (
                history_works(ctx.instance, p, ctx.vars.dates[0], lookback) if lookback else []
            )
            series: list[object] = [*head, *(ctx.vars.worked(p, d) for d in ctx.vars.dates)]
            for start in range(len(series) - cap):
                window = series[start : start + cap + 1]
                if all(isinstance(v, int) for v in window):
                    if sum(window) > cap:  # type: ignore[arg-type]
                        ctx.note(
                            f"{self.id}: pre-horizon history alone already breaks the "
                            f"{cap}-day consecutive limit for {p}; the model will be infeasible"
                        )
                    continue
                ctx.model.add(sum(window) <= cap)  # type: ignore[arg-type]


def _negated_bounded_span(works: list, start: int, length: int) -> list:
    """Official OR-Tools idiom: a literal set that is false exactly when
    ``works[start:start+length]`` is a *maximal* run of True values.

    The optional left/right borders are non-negated neighbours; when the span sits at
    the array boundary there is no border, so a run touching the boundary still counts
    as maximal (which is what we want: a lone day's work at day 0 is still a
    one-day stint).
    """
    seq: list = []
    if start > 0:
        seq.append(works[start - 1])
    seq.extend(~works[start + i] for i in range(length))
    if start + length < len(works):
        seq.append(works[start + length])
    return seq


@register_hard("min_consecutive_work_days")
class MinConsecutiveWorkDays(ConstraintModule):
    """Forbid working streaks shorter than ``days``.

    Implemented as the official ``add_soft_sequence_constraint`` does for its hard
    bound: enumerate every span of length ``1..days-1`` and assert it is not a
    maximal run, via ``add_bool_or(negated_bounded_span(...))``.
    """

    def build(self, ctx: BuildContext) -> None:
        minimum = int(getattr(self.cfg, "days", 2))
        if minimum <= 1:
            return
        for p in ctx.vars.people:
            works: list = [ctx.vars.worked(p, d) for d in ctx.vars.dates]
            for length in range(1, minimum):
                for start in range(len(works) - length + 1):
                    ctx.model.add_bool_or(_negated_bounded_span(works, start, length))


@register_hard("forbid_transition")
class ForbidTransition(ConstraintModule):
    def build(self, ctx: BuildContext) -> None:
        a = str(self.cfg.from_shift)
        b = str(self.cfg.to_shift)
        dates = ctx.vars.dates
        for p in ctx.vars.people:
            for i in range(len(dates) - 1):
                ctx.model.add_bool_or(
                    [~ctx.vars.on(p, dates[i], a), ~ctx.vars.on(p, dates[i + 1], b)]
                )


@register_hard("weekly_rest_days")
class WeeklyRestDays(ConstraintModule):
    def build(self, ctx: BuildContext) -> None:
        need = int(getattr(self.cfg, "min", 1))
        for chunk in period_chunks(ctx.vars.dates, "week"):
            chunk_need = prorated_rest(need, len(chunk))
            if chunk_need != need:
                ctx.note(
                    f"{self.id}: the trailing {len(chunk)}-day partial week is prorated to "
                    f"{chunk_need} rest day(s) out of {need}/week"
                )
            allowance = max(0, len(chunk) - chunk_need)
            for p in ctx.vars.people:
                ctx.model.add(sum(ctx.vars.worked(p, d) for d in chunk) <= allowance)


@register_hard("max_shifts_per_period")
class MaxShiftsPerPeriod(ConstraintModule):
    def build(self, ctx: BuildContext) -> None:
        cap = int(getattr(self.cfg, "max", 1))
        period = str(getattr(self.cfg, "period", "horizon"))
        who = ctx.members(getattr(self.cfg, "group", None))
        for chunk in period_chunks(ctx.vars.dates, period):
            for p in who:
                ctx.model.add(
                    sum(ctx.vars.on(p, d, s) for d in chunk for s in ctx.vars.shifts) <= cap
                )


@register_hard("hours_bounds")
class HoursBounds(ConstraintModule):
    """Weekly or horizon-total working-hours bounds (labour rules, contract hours)."""

    def build(self, ctx: BuildContext) -> None:
        lo = getattr(self.cfg, "min", None)
        hi = getattr(self.cfg, "max", None)
        period = str(getattr(self.cfg, "period", "horizon"))
        who = ctx.members(getattr(self.cfg, "group", None))
        hours = {s.id: s.resolved_hours() for s in ctx.instance.shifts}
        for chunk in period_chunks(ctx.vars.dates, period):
            for p in who:
                expr = sum(
                    round(hours[s] * 1000) * ctx.vars.on(p, d, s)
                    for d in chunk
                    for s in ctx.vars.shifts
                )
                if hi is not None:
                    ctx.model.add(expr <= round(float(hi) * 1000))
                if lo is not None:
                    ctx.model.add(expr >= round(float(lo) * 1000))


@register_hard("requirement")
class Requirement(ConstraintModule):
    """At least ``min`` (and at most ``max``) people from a group on a shift, per day.

    This single primitive expresses "the night shift needs an ICU-certified nurse",
    "every exam room needs a subject expert" and "at least one senior on duty".
    """

    def build(self, ctx: BuildContext) -> None:
        shift = str(self.cfg.shift)
        group = str(self.cfg.group)
        lo = int(getattr(self.cfg, "min", 1))
        hi = getattr(self.cfg, "max", None)
        members = ctx.members(group)
        if shift not in ctx.vars.shifts:
            ctx.note(f"{self.id}: shift {shift!r} does not exist; constraint skipped")
            return
        if len(members) < lo:
            ctx.note(
                f"{self.id}: group {group!r} has {len(members)} members but {lo} are required "
                "on every matching day; the model will be infeasible"
            )
        weekday = getattr(self.cfg, "weekday", None)
        only_dates = {d for d in getattr(self.cfg, "dates", [])}
        for d in ctx.vars.dates:
            if weekday is not None and _WEEKDAY_INDEX[weekday] != d.weekday():
                continue
            if only_dates and d not in only_dates:
                continue
            if ctx.instance.demand_for(d, shift) is None and not _day_relevant(ctx, d, shift):
                continue
            expr = sum(ctx.vars.on(p, d, shift) for p in members)
            ctx.model.add(expr >= lo)
            if hi is not None:
                ctx.model.add(expr <= int(hi))


def _day_relevant(ctx: BuildContext, date: dt.date, shift: str) -> bool:
    """Apply a group requirement on any day the shift is in play at all."""
    return any(
        r.shift == shift and (r.date == date or r.weekday is not None)
        for r in ctx.instance.demand
    )


@register_hard("fixed_assignment")
class FixedAssignment(ConstraintModule):
    def build(self, ctx: BuildContext) -> None:
        person = str(self.cfg.person)
        date = dt.date.fromisoformat(str(self.cfg.date))
        shift = str(self.cfg.shift)
        if date not in ctx.vars.dates:
            ctx.note(f"{self.id}: {date} is outside the horizon; constraint skipped")
            return
        if shift == ctx.vars.rest:
            for real in ctx.vars.shifts:
                ctx.model.add(ctx.vars.on(person, date, real) == 0)
            return
        if shift not in ctx.vars.shifts:
            ctx.note(f"{self.id}: shift {shift!r} does not exist; constraint skipped")
            return
        ctx.model.add(ctx.vars.on(person, date, shift) == 1)


@register_hard("no_pair")
class NoPair(ConstraintModule):
    def build(self, ctx: BuildContext) -> None:
        people = list(self.cfg.people)
        for d in ctx.vars.dates:
            for s in ctx.vars.shifts:
                ctx.model.add(sum(ctx.vars.on(p, d, s) for p in people) <= 1)


@register_hard("pairing")
class Pairing(ConstraintModule):
    """A junior on a shift implies at least one senior on the same shift."""

    def build(self, ctx: BuildContext) -> None:
        juniors = ctx.members(str(self.cfg.junior_group))
        seniors = ctx.members(str(self.cfg.senior_group))
        if not seniors:
            ctx.note(f"{self.id}: senior group is empty; pairing would forbid all juniors")
        for d in ctx.vars.dates:
            for s in ctx.vars.shifts:
                senior_sum: object = sum(ctx.vars.on(q, d, s) for q in seniors) if seniors else 0
                for j in juniors:
                    ctx.model.add(ctx.vars.on(j, d, s) <= senior_sum)


def apply_instance_invariants(ctx: BuildContext) -> None:
    """Enforce rules that live in ``instance`` rather than in the constraint list.

    These are not optional policy, they are facts about the data:

    * approved absences must never be scheduled;
    * a person's declared ``max_shifts`` / ``min_shifts`` caps are hard.

    They were originally only *checked* by the verifier, which meant a spec without
    an explicit constraint list would happily schedule someone who is on leave --
    exactly the class of bug the independent verifier exists to catch.
    """
    inst: Instance = ctx.instance
    dates = set(ctx.vars.dates)
    _forbid_inactive_shifts(ctx, inst, dates)
    _apply_coverage_caps(ctx, inst)

    unavailable = inst.unavailable_set()
    for person, date, shift in sorted(unavailable, key=lambda t: (t[0], t[1], t[2] or "")):
        if date not in dates or person not in ctx.vars.people:
            continue
        targets = ctx.vars.shifts if shift is None else [shift]
        for target in targets:
            if target in ctx.vars.shifts:
                ctx.model.add(ctx.vars.on(person, date, target) == 0)

    for person in inst.people:
        if person.id not in ctx.vars.people:
            continue
        total = sum(
            ctx.vars.on(person.id, d, s) for d in ctx.vars.dates for s in ctx.vars.shifts
        )
        if person.max_shifts is not None:
            ctx.model.add(total <= person.max_shifts)
        if person.min_shifts is not None:
            ctx.model.add(total >= person.min_shifts)


def _apply_coverage_caps(ctx: BuildContext, inst: Instance) -> None:
    """``demand.max`` is a hard ceiling. See ``DemandRule`` for why."""
    for d in ctx.vars.dates:
        for s in ctx.vars.shifts:
            rule = inst.demand_for(d, s)
            if rule is None or rule.max is None:
                continue
            ctx.model.add(
                sum(ctx.vars.on(p, d, s) for p in ctx.vars.people) <= int(rule.max)
            )


def _forbid_inactive_shifts(ctx: BuildContext, inst: Instance, dates: set) -> None:
    """Nobody may work a (date, shift) that has no declared demand.

    Found by inspection of an exported roster: the duty-roster seed declared night
    shifts only on weekends, yet the solver assigned night duty on a Tuesday. The
    cause was that an undeclared (date, shift) had **no constraint at all**, so the
    solver was free to invent work -- a silent wrong answer, not an infeasibility.

    "No demand declared" now means "no work", which is the only safe default. To
    express *optional* staffing, declare the rule explicitly with ``min: 0`` and a
    ``max``; to pin someone regardless, use ``fixed_assignment`` (exempted here).
    """
    if not ctx.vars.shifts:
        return
    fixed: set[tuple[str, object, str]] = set()
    for c in ctx.spec.constraints.hard:
        if getattr(c, "type", "") == "fixed_assignment" and getattr(c, "enabled", True):
            try:
                fixed.add(
                    (
                        str(c.person),
                        dt.date.fromisoformat(str(c.date)),
                        str(c.shift),
                    )
                )
            except ValueError:
                continue
    for d in ctx.vars.dates:
        for s in ctx.vars.shifts:
            if inst.demand_for(d, s) is not None:
                continue
            for p in ctx.vars.people:
                if (p, d, s) in fixed:
                    continue
                ctx.model.add(ctx.vars.on(p, d, s) == 0)


def _unused(*_: object) -> None:  # pragma: no cover - keeps cp_model import honest
    _ = cp_model
