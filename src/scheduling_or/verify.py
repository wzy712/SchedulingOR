"""Independent verifier.

**Never trust the solver's own report.** This module re-implements every hard
constraint in plain Python, reading only the produced assignments and the spec. It
is deliberately *not* shared with the modelling code: an off-by-one in a module
would otherwise be reproduced faithfully by its own checker.

This is also the oracle for property-based tests: any produced solution must verify,
while INFEASIBLE is an acceptable outcome.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .model.vars import prorated_rest
from .schema.instance import _WEEKDAY_INDEX, Instance, Shift
from .schema.solution import Assignment
from .schema.spec import Spec


def _span(date: dt.date, shift: Shift) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(date, shift.start_time)
    end = dt.datetime.combine(date, shift.end_time)
    if shift.crosses_midnight or end <= start:
        end += dt.timedelta(days=1)
    return start, end


def _chunks(dates: list[dt.date], period: str) -> list[list[dt.date]]:
    if period == "horizon":
        return [list(dates)]
    return [dates[i : i + 7] for i in range(0, len(dates), 7)]


def verify(spec: Spec, assignments: list[Assignment]) -> list[str]:
    """Return a list of human-readable violations. Empty list means verified."""
    inst = spec.instance
    errors: list[str] = []
    dates = inst.dates()
    date_set = set(dates)
    shifts = inst.shift_ids
    shift_ids = {s.id for s in inst.shifts}

    by_person: dict[str, dict[dt.date, set[str]]] = defaultdict(lambda: defaultdict(set))
    for a in assignments:
        if a.date not in date_set:
            errors.append(f"assignment outside horizon: {a.person} {a.date} {a.shift}")
            continue
        if a.shift not in shift_ids:
            errors.append(f"assignment references unknown shift: {a.person} {a.date} {a.shift}")
            continue
        if a.shift == inst.rest_code:
            continue
        by_person[a.person][a.date].add(a.shift)

    unknown_people = {p for p in by_person if p not in set(inst.person_ids)}
    for p in sorted(unknown_people):
        errors.append(f"assignment references unknown person {p!r}")

    # --- instance-level invariants (availability) --------------------------
    for a in assignments:
        if a.shift == inst.rest_code:
            continue
        if not inst.is_available(a.person, a.date, a.shift):
            errors.append(f"unavailability violated: {a.person} assigned {a.shift} on {a.date}")

    groups = _safe_groups(inst, errors)

    # --- undeclared (date, shift) must stay empty -------------------------
    # Re-derived here from the spec, not imported from the model: if the modelling
    # side and this check shared an implementation, both would be wrong together.
    fixed_triples: set[tuple[str, dt.date, str]] = set()
    for c in spec.constraints.hard:
        if getattr(c, "type", "") == "fixed_assignment" and getattr(c, "enabled", True):
            try:
                fixed_triples.add(
                    (
                        str(c.person),
                        dt.date.fromisoformat(str(c.date)),
                        str(c.shift),
                    )
                )
            except ValueError:
                continue
    for a in assignments:
        if a.shift == inst.rest_code or a.date not in date_set:
            continue
        if inst.demand_for(a.date, a.shift) is None and (a.person, a.date, a.shift) not in fixed_triples:
            errors.append(
                f"assigned to a shift with no declared demand: {a.person} {a.date} {a.shift}"
            )

    # --- demand ceiling (hard, applied as an instance invariant) ----------
    for d in dates:
        for s in shifts:
            rule = inst.demand_for(d, s)
            if rule is None or rule.max is None:
                continue
            n = sum(1 for p in inst.person_ids if s in by_person.get(p, {}).get(d, set()))
            if n > rule.max:
                errors.append(
                    f"demand ceiling exceeded: {d} {s} has {n} people, max is {rule.max}"
                )

    for c in spec.constraints.hard:
        if not getattr(c, "enabled", True):
            continue
        t = c.type

        if t == "one_shift_per_day":
            cap = int(getattr(c, "max_per_day", 1))
            for p, days in by_person.items():
                for d, ss in days.items():
                    if len(ss) > cap:
                        errors.append(
                            f"{c.id}: {p} has {len(ss)} shifts on {d} (max_per_day={cap}): "
                            f"{sorted(ss)}"
                        )

        elif t == "rest_between_shifts":
            need = float(getattr(c, "min_hours", 0.0))
            if need <= 0:
                continue
            flat: list[tuple[dt.date, Shift]] = []
            for p, days in by_person.items():
                flat = [(d, inst.shift(s)) for d, ss in days.items() for s in ss]
                flat.sort(key=lambda ds: _span(ds[0], ds[1])[0])
                for i in range(len(flat) - 1):
                    _, end1 = _span(*flat[i])
                    start2, _ = _span(*flat[i + 1])
                    gap = (start2 - end1).total_seconds() / 3600.0
                    if gap < need - 1e-9:
                        errors.append(
                            f"{c.id}: {p} has only {gap:.2f}h rest between "
                            f"{flat[i][1].id} on {flat[i][0]} and "
                            f"{flat[i + 1][1].id} on {flat[i + 1][0]} (needs {need}h)"
                        )

        elif t == "max_consecutive_work_days":
            cap = int(getattr(c, "days", 6))
            lookback = cap if bool(getattr(c, "include_history", True)) else 0
            for person in inst.people:
                worked = {h.date for h in person.history} if lookback else set()
                worked |= set(by_person.get(person.id, {}))
                streak = best = 0
                cursor = min(worked) if worked else dates[0]
                if lookback:
                    cursor = dates[0] - dt.timedelta(days=lookback)
                span = (max(dates[-1], max(worked, default=dates[-1])) - cursor).days + 1
                for i in range(span):
                    day = cursor + dt.timedelta(days=i)
                    if day in worked:
                        streak += 1
                        best = max(best, streak)
                    else:
                        streak = 0
                if best > cap:
                    errors.append(
                        f"{c.id}: {person.id} worked {best} consecutive days (max {cap})"
                    )

        elif t == "min_consecutive_work_days":
            minimum = int(getattr(c, "days", 2))
            if minimum <= 1:
                continue
            for p in inst.person_ids:
                days_worked = [d for d in dates if by_person.get(p, {}).get(d)]
                if not days_worked:
                    continue
                runs: list[int] = []
                run = 1
                for i in range(1, len(days_worked)):
                    if (days_worked[i] - days_worked[i - 1]).days == 1:
                        run += 1
                    else:
                        runs.append(run)
                        run = 1
                runs.append(run)
                short = [r for r in runs if r < minimum]
                if short:
                    errors.append(
                        f"{c.id}: {p} has working streak(s) of {short} day(s), "
                        f"below the minimum of {minimum}"
                    )

        elif t == "forbid_transition":
            a_s, b_s = str(c.from_shift), str(c.to_shift)
            for p, days in by_person.items():
                for i in range(len(dates) - 1):
                    if a_s in days.get(dates[i], set()) and b_s in days.get(dates[i + 1], set()):
                        errors.append(
                            f"{c.id}: {p} has forbidden transition {a_s}->{b_s} "
                            f"on {dates[i]}->{dates[i + 1]}"
                        )

        elif t == "weekly_rest_days":
            need = int(getattr(c, "min", 1))
            for chunk in _chunks(dates, "week"):
                chunk_need = prorated_rest(need, len(chunk))
                for p in inst.person_ids:
                    worked_days = sum(1 for d in chunk if by_person.get(p, {}).get(d))
                    rest = len(chunk) - worked_days
                    if rest < chunk_need:
                        errors.append(
                            f"{c.id}: {p} has only {rest} rest day(s) in week starting "
                            f"{chunk[0]} (needs {chunk_need})"
                        )

        elif t == "max_shifts_per_period":
            cap = int(getattr(c, "max", 1))
            period = str(getattr(c, "period", "horizon"))
            who = _members(groups, getattr(c, "group", None), inst, errors, c.id)
            for chunk in _chunks(dates, period):
                for p in who:
                    n = sum(len(by_person.get(p, {}).get(d, set())) for d in chunk)
                    if n > cap:
                        errors.append(
                            f"{c.id}: {p} has {n} shifts in period starting {chunk[0]} (max {cap})"
                        )

        elif t == "hours_bounds":
            lo = getattr(c, "min", None)
            hi = getattr(c, "max", None)
            period = str(getattr(c, "period", "horizon"))
            who = _members(groups, getattr(c, "group", None), inst, errors, c.id)
            for chunk in _chunks(dates, period):
                for p in who:
                    total = sum(
                        inst.shift(s).resolved_hours() for d in chunk for s in by_person.get(p, {}).get(d, set())
                    )
                    if hi is not None and total > float(hi) + 1e-6:
                        errors.append(
                            f"{c.id}: {p} works {total:.2f}h in period starting {chunk[0]} "
                            f"(max {hi}h)"
                        )
                    if lo is not None and total < float(lo) - 1e-6:
                        errors.append(
                            f"{c.id}: {p} works {total:.2f}h in period starting {chunk[0]} "
                            f"(min {lo}h)"
                        )

        elif t == "requirement":
            shift = str(c.shift)
            lo = int(getattr(c, "min", 1))
            hi = getattr(c, "max", None)
            members = _members(groups, c.group, inst, errors, c.id)
            weekday = getattr(c, "weekday", None)
            only_dates = {d for d in getattr(c, "dates", [])}
            for d in dates:
                if weekday is not None and _WEEKDAY_INDEX[weekday] != d.weekday():
                    continue
                if only_dates and d not in only_dates:
                    continue
                if inst.demand_for(d, shift) is None or shift not in shifts:
                    continue
                n = sum(1 for p in members if shift in by_person.get(p, {}).get(d, set()))
                if n < lo:
                    errors.append(
                        f"{c.id}: on {d} only {n} of group {c.group} work {shift} "
                        f"(needs {lo})"
                    )
                if hi is not None and n > int(hi):
                    errors.append(
                        f"{c.id}: on {d} {n} of group {c.group} work {shift} "
                        f"(max {hi})"
                    )

        elif t == "fixed_assignment":
            person = str(c.person)
            date = dt.date.fromisoformat(str(c.date))
            shift = str(c.shift)
            if date not in date_set:
                continue
            actual = by_person.get(person, {}).get(date, set())
            if shift == inst.rest_code:
                if actual:
                    errors.append(f"{c.id}: {person} must rest on {date} but works {sorted(actual)}")
            elif shift not in actual:
                errors.append(f"{c.id}: {person} must work {shift} on {date} but does not")

        elif t == "no_pair":
            people = list(c.people)
            for d in dates:
                for s in shifts:
                    together = [p for p in people if s in by_person.get(p, {}).get(d, set())]
                    if len(together) > 1:
                        errors.append(f"{c.id}: {together} share {s} on {d}")

        elif t == "pairing":
            juniors = _members(groups, c.junior_group, inst, errors, c.id)
            seniors = _members(groups, c.senior_group, inst, errors, c.id)
            for d in dates:
                for s in shifts:
                    junior_present = any(
                        s in by_person.get(j, {}).get(d, set()) for j in juniors
                    )
                    senior_present = any(
                        s in by_person.get(q, {}).get(d, set()) for q in seniors
                    )
                    if junior_present and not senior_present:
                        errors.append(
                            f"{c.id}: a junior works {s} on {d} with no senior present"
                        )

    return errors


def _safe_groups(
    inst: Instance, errors: list[str]
) -> dict[str, list[str]]:
    try:
        return inst.resolved_groups()
    except Exception as exc:
        errors.append(f"groups could not be resolved: {exc}")
        return {}


def _members(
    groups: dict[str, list[str]],
    group_id: str | None,
    inst: Instance,
    errors: list[str],
    cid: str,
) -> list[str]:
    if group_id is None:
        return list(inst.person_ids)
    if group_id not in groups:
        errors.append(f"{cid}: unknown group {group_id!r}")
        return []
    return groups[group_id]
