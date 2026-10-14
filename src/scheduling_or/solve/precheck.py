"""Arithmetic feasibility pre-check.

Measured lesson (see docs/research/01-existing-projects.md, SO 57139457): the
slowest cases are not "hard to optimise", they are **infeasible** -- the solver can
burn many minutes proving something a few sums would have shown in a millisecond.
So this runs before CP-SAT is ever invoked.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..schema.instance import Instance
from ..schema.spec import Spec


@dataclass
class Problem:
    code: str
    message: str
    date: dt.date | None = None
    shift: str | None = None
    people: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "date": self.date.isoformat() if self.date else None,
            "shift": self.shift,
            "people": self.people,
        }


@dataclass
class Precheck:
    ok: bool
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return f"pre-check passed ({len(self.warnings)} warning(s))"
        return f"pre-check found {len(self.problems)} blocking problem(s)"

    def suggestions(self) -> list[str]:
        out: list[str] = []
        for p in self.problems:
            if p.code == "shift_understaffed":
                out.append(
                    f"{p.date} 的 {p.shift} 班次需要的人手超过当天可用人数，"
                    "建议降低需求、增加可用人员或放宽该班次的资质要求"
                )
            elif p.code == "day_over_capacity":
                out.append(
                    f"{p.date} 全天需求超过「人数 × 每人每日可排班次数」的上限，"
                    "建议提高 one_shift_per_day.max_per_day 或减少当天需求"
                )
            elif p.code == "horizon_over_capacity":
                out.append("整个周期的需求总量超过人力总容量，建议缩短周期、增加人员或降低需求")
            elif p.code == "person_over_max_shifts":
                out.append(
                    f"人员 {p.people} 被固定的班次已超过其 max_shifts，建议调高上限或改固定安排"
                )
            elif p.code == "history_streak":
                out.append(
                    f"人员 {p.people} 的历史班次已连续超过上限，本月开头必须安排休息"
                )
            elif p.code == "hours_over_capacity":
                out.append("月度最少工时要求超过人力可提供的总工时，建议降低 min 或增加人员")
            elif p.code == "hours_floor_exceeds_demand":
                out.append(
                    "工时下限会让所有人必须上班，但总需求不够分：建议去掉 hours_bounds.min，"
                    "或把它改成 horizon 周期 / 只对某个组生效"
                )
        # de-duplicate, keep order
        seen: set[str] = set()
        uniq: list[str] = []
        for s in out:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        return uniq


def _max_per_day(spec: Spec) -> int:
    cap = 1
    for c in spec.constraints.hard:
        if getattr(c, "type", "") == "one_shift_per_day" and getattr(c, "enabled", True):
            cap = max(cap, int(getattr(c, "max_per_day", 1)))
    return cap


def _available(instance: Instance, person: str, date: dt.date, shift: str) -> bool:
    return instance.is_available(person, date, shift)


def run_precheck(spec: Spec) -> Precheck:
    inst = spec.instance
    dates = inst.dates()
    shifts = inst.shift_ids
    people = inst.person_ids
    cap = _max_per_day(spec)
    problems: list[Problem] = []
    warnings: list[str] = []

    # --- per (date, shift) -------------------------------------------------
    for d in dates:
        for s in shifts:
            rule = inst.demand_for(d, s)
            if rule is None:
                continue
            floor = rule.min if rule.min > 0 else (rule.target or 0)
            if floor <= 0:
                continue
            able = [p for p in people if _available(inst, p, d, s)]
            if len(able) < floor:
                problems.append(
                    Problem(
                        code="shift_understaffed",
                        message=(
                            f"{d.isoformat()} 的 {s} 需要至少 {floor} 人，"
                            f"但当天只有 {len(able)} 人可用"
                        ),
                        date=d,
                        shift=s,
                        people=able,
                    )
                )

    # --- per day capacity --------------------------------------------------
    for d in dates:
        need = 0
        for s in shifts:
            rule = inst.demand_for(d, s)
            if rule is None:
                continue
            need += rule.min if rule.min > 0 else (rule.target or 0)
        able = [p for p in people if _available(inst, p, d, s) or True]  # day-level
        day_able = [
            p for p in people if any(_available(inst, p, d, s) for s in shifts)
        ]
        capacity = len(day_able) * cap
        if need > capacity:
            problems.append(
                Problem(
                    code="day_over_capacity",
                    message=(
                        f"{d.isoformat()} 全天需要 {need} 人班，"
                        f"但最多只能提供 {capacity}（{len(day_able)} 人 × 每人每日 {cap} 班）"
                    ),
                    date=d,
                    people=day_able,
                )
            )
        del able

    # --- horizon capacity --------------------------------------------------
    total_need = 0
    for d in dates:
        for s in shifts:
            rule = inst.demand_for(d, s)
            if rule is None:
                continue
            total_need += rule.min if rule.min > 0 else (rule.target or 0)

    per_person_cap: dict[str, int] = {}
    for p in inst.people:
        cap_i = len(dates) * cap
        if p.max_shifts is not None:
            cap_i = min(cap_i, p.max_shifts)
        per_person_cap[p.id] = cap_i
    for c in spec.constraints.hard:
        if getattr(c, "type", "") == "max_shifts_per_period" and getattr(c, "enabled", True):
            maxv = int(getattr(c, "max", 0))
            period = str(getattr(c, "period", "horizon"))
            factor = 1 if period == "horizon" else max(1, len(dates) // 7 + (1 if len(dates) % 7 else 0))
            group = getattr(c, "group", None)
            targets = inst.resolved_groups().get(group, people) if group else people
            for p in targets:
                per_person_cap[p] = min(per_person_cap[p], maxv * factor)
    total_capacity = sum(per_person_cap.values())
    if total_need > total_capacity:
        problems.append(
            Problem(
                code="horizon_over_capacity",
                message=(
                    f"周期内需求总量 {total_need} 超过人力总容量 {total_capacity}"
                ),
            )
        )

    # --- explicit per-person caps vs fixed assignments ----------------------
    fixed_by_person: dict[str, int] = {}
    for c in spec.constraints.hard:
        if (
            getattr(c, "type", "") == "fixed_assignment"
            and getattr(c, "enabled", True)
            and str(c.shift) != inst.rest_code
        ):
            fixed_by_person[str(c.person)] = fixed_by_person.get(str(c.person), 0) + 1
    for p in inst.people:
        n_fixed = fixed_by_person.get(p.id, 0)
        if p.max_shifts is not None and n_fixed > p.max_shifts:
            problems.append(
                Problem(
                    code="person_over_max_shifts",
                    message=(
                        f"{p.name or p.id} 有 {n_fixed} 个固定班次，超过 max_shifts={p.max_shifts}"
                    ),
                    people=[p.id],
                )
            )

    # --- pre-horizon consecutive-day streaks -------------------------------
    cap_days = None
    include_history = True
    for c in spec.constraints.hard:
        if getattr(c, "type", "") == "max_consecutive_work_days" and getattr(c, "enabled", True):
            cap_days = int(getattr(c, "days", 6))
            include_history = bool(getattr(c, "include_history", True))
    if cap_days is not None and include_history:
        start = dates[0]
        for p in inst.people:
            worked = {h.date for h in p.history}
            streak = 0
            probe = start - dt.timedelta(days=1)
            while probe in worked and streak < cap_days + 1:
                streak += 1
                probe -= dt.timedelta(days=1)
            if streak > cap_days:
                problems.append(
                    Problem(
                        code="history_streak",
                        message=(
                            f"{p.name or p.id} 在周期开始前已连续工作 {streak} 天，"
                            f"超过上限 {cap_days} 天"
                        ),
                        people=[p.id],
                    )
                )
            elif streak == cap_days:
                warnings.append(
                    f"{p.name or p.id} 在周期开始前已连续工作 {cap_days} 天，第一天必须休息"
                )

    # --- minimum hours vs capacity ----------------------------------------
    hours = {s.id: s.resolved_hours() for s in inst.shifts}
    for c in spec.constraints.hard:
        if getattr(c, "type", "") == "hours_bounds" and getattr(c, "enabled", True):
            lo = getattr(c, "min", None)
            if lo is None:
                continue
            who = inst.resolved_groups().get(c.group, None) if getattr(c, "group", None) else people
            max_hours_per_person = 0.0
            for p_str in who:
                p = next(x for x in inst.people if x.id == p_str)
                n = per_person_cap[p_str]
                top = max(hours.values()) if hours else 0.0
                max_hours_per_person = max(max_hours_per_person, n * top * p.fte)
            if float(lo) > max_hours_per_person:
                problems.append(
                    Problem(
                        code="hours_over_capacity",
                        message=(
                            f"最少工时要求 {lo}h 超过单人可提供的最大工时 "
                            f"{max_hours_per_person:.1f}h"
                        ),
                    )
                )

    # --- minimum hours floor vs actual demand ------------------------------
    # A per-period `minutes` floor forces *everyone* to work, which can demand more
    # person-hours than the roster actually contains. Arithmetic catches it instantly;
    # CP-SAT would spend a full solve proving it unsatisfiable.
    demand_hours = 0.0
    for d in dates:
        for s in shifts:
            rule = inst.demand_for(d, s)
            if rule is None:
                continue
            floor = rule.min if rule.min > 0 else (rule.target or 0)
            demand_hours += floor * hours.get(s, 0.0)
    for c in spec.constraints.hard:
        if getattr(c, "type", "") != "hours_bounds" or not getattr(c, "enabled", True):
            continue
        lo = getattr(c, "min", None)
        if lo is None or float(lo) <= 0:
            continue
        period = str(getattr(c, "period", "horizon"))
        group = getattr(c, "group", None)
        who = inst.resolved_groups().get(group, people) if group else people
        n_periods = (
            1
            if period == "horizon"
            else max(1, len(dates) // 7 + (1 if len(dates) % 7 else 0))
        )
        required = float(lo) * n_periods * len(who)
        if required > demand_hours + 1e-6:
            problems.append(
                Problem(
                    code="hours_floor_exceeds_demand",
                    message=(
                        f"工时下限要求每人每{('月' if period == 'horizon' else '周')}至少 {lo}h，"
                        f"{len(who)} 人 × {n_periods} 个周期 = {required:.0f}h，"
                        f"但全周期实际只有 {demand_hours:.0f}h 的排班需求"
                    ),
                    people=list(who),
                )
            )

    return Precheck(
        ok=not problems,
        problems=problems,
        warnings=warnings,
        stats={
            "dates": len(dates),
            "people": len(people),
            "shifts": len(shifts),
            "total_required_assignments": total_need,
            "total_capacity": total_capacity,
            "max_per_day": cap,
        },
    )
