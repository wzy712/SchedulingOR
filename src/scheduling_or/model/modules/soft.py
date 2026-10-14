"""Soft constraints: violating these costs something and shows up in the report.

Encoding rules used throughout this module:

* A violation is modelled as a bounded non-negative variable with
  ``violation >= rhs - expr``. The variable's **domain upper bound is the big-M** --
  never hand-write one. A hand-written M that is too small silently truncates the
  constraint (measured failure mode, see docs/research/02-cpsat-modeling.md).
* Nothing here calls ``model.minimize``; every penalty goes into the ledger so the
  objective stays decomposable and reportable.
"""

from __future__ import annotations

import datetime as dt

from ...schema.instance import DemandRule
from ..vars import period_chunks
from .base import BuildContext, ConstraintModule, register_soft


def _floor(rule: DemandRule) -> int:
    if rule.min > 0:
        return rule.min
    return rule.target if rule.target is not None else 0


def _ceiling(rule: DemandRule) -> int | None:
    """The level above which staffing is *wasted*.

    Deliberately ``target`` rather than ``max``: ``max`` is now a hard cap, so an
    excess measured against it could never be positive. Pricing the gap between
    ``target`` and ``max`` is what lets a spec express "we can go up to 3, but we
    would rather not pay for the third person".
    """
    if rule.target is not None:
        return rule.target
    return rule.min if rule.min > 0 else None


@register_soft("coverage_shortfall")
class CoverageShortfall(ConstraintModule):
    """Penalise being below the demand floor.

    This should carry the dominant weight: under-staffing is the failure the user
    actually cares about. Note the deliberate asymmetry with a hard coverage rule --
    making coverage hard is measured to be ~12x slower (google/or-tools#1102), so the
    default is a big penalty rather than a hard constraint.
    """

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 1000))
        if weight == 0:
            return
        for d in ctx.vars.dates:
            for s in ctx.vars.shifts:
                rule = ctx.instance.demand_for(d, s)
                if rule is None:
                    continue
                floor = _floor(rule)
                if floor <= 0:
                    continue
                staffed = sum(ctx.vars.on(p, d, s) for p in ctx.vars.people)
                short = ctx.model.new_int_var(0, floor, f"short_{d.isoformat()}_{s}")
                ctx.model.add(short >= floor - staffed)
                ctx.ledger.add(
                    self.id,
                    self.family,
                    short,
                    weight,
                    label=f"{d.isoformat()} {s} below {floor}",
                )


@register_soft("coverage_excess")
class CoverageExcess(ConstraintModule):
    """Penalise being above the demand ceiling (over-staffing wastes capacity)."""

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 20))
        if weight == 0:
            return
        n_people = len(ctx.vars.people)
        for d in ctx.vars.dates:
            for s in ctx.vars.shifts:
                rule = ctx.instance.demand_for(d, s)
                if rule is None:
                    continue
                ceiling = _ceiling(rule)
                if ceiling is None or ceiling >= n_people:
                    continue
                staffed = sum(ctx.vars.on(p, d, s) for p in ctx.vars.people)
                over = ctx.model.new_int_var(0, n_people - ceiling, f"over_{d.isoformat()}_{s}")
                ctx.model.add(over >= staffed - ceiling)
                ctx.ledger.add(
                    self.id,
                    self.family,
                    over,
                    weight,
                    label=f"{d.isoformat()} {s} above {ceiling}",
                )


@register_soft("skill_shortfall")
class SkillShortfall(ConstraintModule):
    """Soft requirement: penalise each day the qualified group is below ``min``.

    The hard/soft split matters. A hard ``requirement`` says "this roster is
    unpublishable without a certified nurse"; a ``skill_shortfall`` says "we will
    accept it, but tell us and price it". Both are legitimate; the user picks.
    """

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 800))
        if weight == 0:
            return
        shift = str(self.cfg.shift)
        group = str(self.cfg.group)
        need = int(getattr(self.cfg, "min", 1))
        if shift not in ctx.vars.shifts:
            ctx.note(f"{self.id}: shift {shift!r} does not exist; constraint skipped")
            return
        members = ctx.members(group)
        for d in ctx.vars.dates:
            if ctx.instance.demand_for(d, shift) is None:
                continue
            got = sum(ctx.vars.on(p, d, shift) for p in members)
            short = ctx.model.new_int_var(0, need, f"skill_short_{d.isoformat()}_{shift}")
            ctx.model.add(short >= need - got)
            ctx.ledger.add(
                self.id,
                self.family,
                short,
                weight,
                label=f"{d.isoformat()} {shift}: below {need} from group {group}",
            )


@register_soft("fairness")
class Fairness(ConstraintModule):
    """Balance a workload metric across people.

    ``spread`` minimises max - min. ``deviation`` minimises the sum of absolute
    deviations from the mean; because the mean is not an integer in general, the
    deviations are computed as ``|n * count - total|`` and are therefore scaled by
    ``n``. The report always shows the *unscaled* numbers, so the weight stays
    interpretable.
    """

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 50))
        if weight == 0:
            return
        metric = str(getattr(self.cfg, "metric", "total_shifts"))
        aggregation = str(getattr(self.cfg, "aggregation", "spread"))
        people = ctx.members(getattr(self.cfg, "group", None))
        if len(people) < 2:
            ctx.note(f"{self.id}: fairness needs at least two people; metric {metric} skipped")
            return

        counts: list[tuple[str, object]] = []
        max_count = 0
        for p in people:
            if metric == "total_shifts":
                expr = sum(
                    ctx.vars.on(p, d, s) for d in ctx.vars.dates for s in ctx.vars.shifts
                )
                upper = len(ctx.vars.dates) * len(ctx.vars.shifts)
            elif metric == "weekend_shifts":
                wdays = [d for d in ctx.vars.dates if d.weekday() >= 5]
                expr = sum(ctx.vars.on(p, d, s) for d in wdays for s in ctx.vars.shifts)
                upper = len(wdays) * len(ctx.vars.shifts)
            elif metric == "holiday_shifts":
                hdays = [d for d in ctx.vars.dates if d in set(ctx.instance.holidays)]
                expr = sum(ctx.vars.on(p, d, s) for d in hdays for s in ctx.vars.shifts)
                upper = len(hdays) * len(ctx.vars.shifts)
            elif metric.startswith("shift:"):
                sid = metric.split(":", 1)[1]
                if sid not in ctx.vars.shifts:
                    ctx.note(f"{self.id}: fairness metric references unknown shift {sid!r}")
                    return
                expr = sum(ctx.vars.on(p, d, sid) for d in ctx.vars.dates)
                upper = len(ctx.vars.dates)
            else:
                ctx.note(f"{self.id}: unsupported fairness metric {metric!r}")
                return
            var = ctx.model.new_int_var(0, upper, f"fair_{metric.replace(':', '_')}_{p}")
            ctx.model.add(var == expr)
            counts.append((p, var))
            max_count = max(max_count, upper)

        if aggregation == "spread":
            mx = ctx.model.new_int_var(0, max_count, f"fair_max_{self.id}")
            mn = ctx.model.new_int_var(0, max_count, f"fair_min_{self.id}")
            ctx.model.add_max_equality(mx, [c for _, c in counts])
            ctx.model.add_min_equality(mn, [c for _, c in counts])
            spread = ctx.model.new_int_var(0, max_count, f"fair_spread_{self.id}")
            ctx.model.add(spread == mx - mn)
            ctx.ledger.add(
                self.id, self.family, spread, weight, label=f"{metric} max-min spread"
            )
        else:
            n = len(counts)
            total = ctx.model.new_int_var(0, n * max_count, f"fair_total_{self.id}")
            ctx.model.add(total == sum(c for _, c in counts))
            devs = []
            for p, c in counts:
                dev = ctx.model.new_int_var(0, n * max_count, f"fair_dev_{self.id}_{p}")
                ctx.model.add(dev >= n * c - total)
                ctx.model.add(dev >= total - n * c)
                devs.append(dev)
            ctx.ledger.add(
                self.id,
                self.family,
                sum(devs),
                weight,
                label=f"{metric} total absolute deviation (x{n})",
            )


@register_soft("preference")
class Preference(ConstraintModule):
    """Reward wanted assignments and penalise unwanted ones.

    Both directions become a *minimised violation*: for a wanted shift the
    violation is ``1 - x``; for an avoided shift it is ``x``. Keeping the sign
    convention uniform is what lets the ledger stay a plain sum.
    """

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 30))
        if weight == 0:
            return
        applied = 0
        for i, pref in enumerate(ctx.instance.preferences):
            if pref.shift not in ctx.vars.shifts:
                ctx.note(f"{self.id}: preference references unknown shift {pref.shift!r}")
                continue
            for d in ctx.vars.dates:
                if not pref.matches(d):
                    continue
                x = ctx.vars.on(pref.person, d, pref.shift)
                w = weight * pref.weight
                if pref.kind == "want":
                    unmet = ctx.model.new_bool_var(
                        f"unmet_{i}_{pref.person}_{d.isoformat()}_{pref.shift}"
                    )
                    ctx.model.add(unmet + x == 1)
                    ctx.ledger.add(
                        self.id,
                        self.family,
                        unmet,
                        w,
                        label=f"{pref.person} wanted {pref.shift} on {d.isoformat()}",
                    )
                else:
                    ctx.ledger.add(
                        self.id,
                        self.family,
                        x,
                        w,
                        label=f"{pref.person} avoided {pref.shift} on {d.isoformat()}",
                    )
                applied += 1
        if applied == 0 and ctx.instance.preferences:
            ctx.note(f"{self.id}: no preference matched the horizon")


@register_soft("consecutive_work_preference")
class ConsecutiveWorkPreference(ConstraintModule):
    """Softly discourage working streaks longer than ``preferred_max``.

    Implementation note: penalising ``max(0, window_sum - preferred_max)`` over every
    sliding window of size ``preferred_max + 1`` charges a streak of length L exactly
    ``L - preferred_max`` units, which is the intended linear penalty and stays a
    plain linear expression (no span enumeration needed).
    """

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 10))
        pm = int(getattr(self.cfg, "preferred_max", 5))
        if weight == 0:
            return
        for p in ctx.vars.people:
            for start in range(len(ctx.vars.dates) - pm):
                window = [ctx.vars.worked(p, d) for d in ctx.vars.dates[start : start + pm + 1]]
                excess = ctx.model.new_int_var(0, pm + 1, f"cw_{p}_{start}")
                ctx.model.add(excess >= sum(window) - pm)
                ctx.ledger.add(
                    self.id,
                    self.family,
                    excess,
                    weight,
                    label=f"{p} streak beyond {pm} days starting {ctx.vars.dates[start].isoformat()}",
                )


@register_soft("penalized_transition")
class PenalizedTransition(ConstraintModule):
    """Discourage a shift sequence without forbidding it (e.g. afternoon -> night)."""

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 10))
        a = str(self.cfg.from_shift)
        b = str(self.cfg.to_shift)
        if weight == 0 or a not in ctx.vars.shifts or b not in ctx.vars.shifts:
            if a not in ctx.vars.shifts or b not in ctx.vars.shifts:
                ctx.note(f"{self.id}: transition references an unknown shift; skipped")
            return
        dates = ctx.vars.dates
        for p in ctx.vars.people:
            for i in range(len(dates) - 1):
                t = ctx.model.new_int_var(0, 1, f"trans_{p}_{i}")
                ctx.model.add(
                    t >= ctx.vars.on(p, dates[i], a) + ctx.vars.on(p, dates[i + 1], b) - 1
                )
                ctx.ledger.add(
                    self.id,
                    self.family,
                    t,
                    weight,
                    label=f"{p} {a}->{b} on {dates[i].isoformat()}",
                )


@register_soft("target_shifts_deviation")
class TargetShiftsDeviation(ConstraintModule):
    """Pull each person's shift count toward an explicit target (contract hours)."""

    def build(self, ctx: BuildContext) -> None:
        weight = int(getattr(self.cfg, "weight", 30))
        default_target = getattr(self.cfg, "default_target", None)
        who = ctx.members(getattr(self.cfg, "group", None))
        if weight == 0:
            return
        by_id = {p.id: p for p in ctx.instance.people}
        for p in who:
            person = by_id[p]
            target = person.target_shifts if person.target_shifts is not None else default_target
            if target is None:
                continue
            upper = len(ctx.vars.dates) * len(ctx.vars.shifts)
            count = ctx.model.new_int_var(0, upper, f"tsd_count_{p}")
            ctx.model.add(
                count == sum(ctx.vars.on(p, d, s) for d in ctx.vars.dates for s in ctx.vars.shifts)
            )
            dev = ctx.model.new_int_var(0, upper, f"tsd_dev_{p}")
            ctx.model.add(dev >= count - int(target))
            ctx.model.add(dev >= int(target) - count)
            ctx.ledger.add(
                self.id, self.family, dev, weight, label=f"{p} deviation from target {target}"
            )


# Kept so the module remains importable even if all soft constraints are disabled.
_DEFAULT_WINDOW = period_chunks([dt.date(2025, 1, 1)], "week")
