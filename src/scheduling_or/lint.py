"""Data-quality lint.

Runs before anything else and answers one question: *is this data good enough to
model?* Problems are reported with the offending person/date, because the reader is
a 教务员 or a 护士长, not an engineer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as dt_date_fromisoformat
from typing import Literal

from .schema.instance import Instance
from .schema.spec import Spec

Level = Literal["error", "warning", "info"]


@dataclass
class Issue:
    level: Level
    code: str
    message: str
    hint: str = ""
    where: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "where": self.where,
        }


@dataclass
class LintReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"lint: {len(self.errors)} error(s), {len(self.warnings)} warning(s), "
            f"{len(self.issues) - len(self.errors) - len(self.warnings)} info"
        )


def lint_instance(instance: Instance) -> LintReport:
    rep = LintReport()
    dates = instance.dates()
    date_set = set(dates)
    shifts = instance.shift_ids

    if not instance.people:
        rep.issues.append(Issue("error", "no_people", "人员名单为空，无法排班"))
    if not shifts:
        rep.issues.append(
            Issue("error", "no_shifts", "没有任何需要排班的班次（所有 shift 的 attendance 都是 false）")
        )
    if not instance.demand:
        rep.issues.append(
            Issue(
                "error",
                "no_demand",
                "没有定义任何需求（demand），求解器会把所有人都排成休息",
                hint="至少给出每个班次的最小人数",
            )
        )
    if not instance.groups:
        rep.issues.append(
            Issue(
                "info",
                "no_groups",
                "没有定义任何组（group）；用组表达资质/岗位要求，人员变动时不必改约束",
            )
        )

    # --- demand sanity ----------------------------------------------------
    covered: dict[str, int] = {s: 0 for s in shifts}
    for rule in instance.demand:
        if rule.shift not in shifts:
            rep.issues.append(
                Issue(
                    "error",
                    "demand_unknown_shift",
                    f"需求引用了不存在的班次 {rule.shift!r}",
                    where=f"demand[{rule.shift}]",
                )
            )
            continue
        covered[rule.shift] += 1
        if rule.target is not None and rule.min == 0 and rule.target == 0:
            rep.issues.append(
                Issue(
                    "warning",
                    "demand_all_zero",
                    f"班次 {rule.shift} 的需求 min/target 都是 0，这条规则不会产生任何约束",
                    where=f"demand[{rule.shift}]",
                )
            )
    for s, n in covered.items():
        if n == 0:
            rep.issues.append(
                Issue(
                    "warning",
                    "shift_without_demand",
                    f"班次 {s} 从未出现在任何需求规则中，它只会被当作可选工作",
                    hint="如果这个班次确实不需要人，建议从 shifts 中移除或把 attendance 设为 false",
                    where=s,
                )
            )

    # --- days where a used shift has no demand rule -----------------------
    # Undeclared (date, shift) is forbidden by default, so a shift declared for some
    # days but not others silently cannot be staffed on the others. That is easy to
    # write by accident and impossible to see in the roster.
    used_shifts = {r.shift for r in instance.demand if r.shift in shifts}
    for s in sorted(used_shifts):
        missing_days = [d for d in dates if instance.demand_for(d, s) is None]
        if not missing_days:
            continue
        sample = "、".join(d.isoformat() for d in missing_days[:3])
        more = f" 等 {len(missing_days)} 天" if len(missing_days) > 3 else ""
        rep.issues.append(
            Issue(
                "warning",
                "shift_days_without_demand",
                f"班次 {s} 在 {sample}{more} 没有需求规则，这些天将不允许排该班次",
                hint="没有需求声明 = 不需要人。若这些天确实不需要该班次，可忽略；"
                "否则请补 demand 规则（weekday 或具体 date）",
                where=s,
            )
        )

    # --- weekday coverage gaps -------------------------------------------
    weekdays_present = {r.weekday for r in instance.demand if r.weekday is not None}
    weekdays_in_horizon = {["mon", "tue", "wed", "thu", "fri", "sat", "sun"][d.weekday()] for d in dates}
    missing = weekdays_in_horizon - weekdays_present
    if missing and not any(r.date is not None for r in instance.demand):
        rep.issues.append(
            Issue(
                "warning",
                "weekday_gap",
                f"周期内出现了这些星期几但没有任何需求规则覆盖：{sorted(missing)}",
                hint="未覆盖的日期将不会产生覆盖约束",
            )
        )

    # --- unavailability ---------------------------------------------------
    for rule in instance.unavailability:
        ds = rule.resolved_dates()
        outside = [d for d in ds if d not in date_set]
        if outside and len(outside) == len(ds):
            rep.issues.append(
                Issue(
                    "warning",
                    "unavailability_outside_horizon",
                    f"{rule.person} 的 {rule.reason or '不可用'} 全部落在排班周期之外，不产生任何影响",
                    where=rule.person,
                )
            )
        for s in rule.shifts:
            if s not in shifts:
                rep.issues.append(
                    Issue(
                        "error",
                        "unavailability_unknown_shift",
                        f"{rule.person} 的不可用记录引用了不存在的班次 {s!r}",
                        where=rule.person,
                    )
                )
    avail_days = {}
    for p in instance.people:
        n = sum(1 for d in dates for s in shifts if instance.is_available(p.id, d, s))
        avail_days[p.id] = n
        if n == 0:
            rep.issues.append(
                Issue(
                    "error",
                    "person_fully_unavailable",
                    f"{p.name or p.id} 在整个周期内没有任何可排班时间",
                    hint="检查 unavailability 与 person.unavailable 是否写错",
                    where=p.id,
                )
            )

    # --- per-person capacity ---------------------------------------------
    total_demand_min = 0
    for d in dates:
        for s in shifts:
            rule = instance.demand_for(d, s)
            if rule is None:
                continue
            total_demand_min += rule.min if rule.min > 0 else (rule.target or 0)
    total_avail = sum(avail_days.values())
    if total_demand_min > 0 and total_avail == 0:
        rep.issues.append(Issue("error", "no_capacity", "没有任何可用的排班容量"))
    for p in instance.people:
        if p.min_shifts is not None and p.max_shifts is not None and p.min_shifts > p.max_shifts:
            rep.issues.append(
                Issue(
                    "error",
                    "person_min_gt_max",
                    f"{p.name or p.id}: min_shifts={p.min_shifts} 大于 max_shifts={p.max_shifts}",
                    where=p.id,
                )
            )
        if p.fte != 1.0:
            rep.issues.append(
                Issue(
                    "info",
                    "fte_not_enforced",
                    f"{p.name or p.id} 的 fte={p.fte}；当前版本只用它做参考，"
                    "不会自动按比例折算需求，请用 hours_bounds 或 max_shifts 表达",
                    where=p.id,
                )
            )

    # --- history ----------------------------------------------------------
    horizon_start = dates[0]
    for p in instance.people:
        for h in p.history:
            if h.shift not in shifts:
                rep.issues.append(
                    Issue(
                        "error",
                        "history_unknown_shift",
                        f"{p.name or p.id} 的历史记录引用了不存在的班次 {h.shift!r}",
                        where=p.id,
                    )
                )
            if h.date >= horizon_start:
                rep.issues.append(
                    Issue(
                        "warning",
                        "history_inside_horizon",
                        f"{p.name or p.id} 的历史记录 {h.date} 落在排班周期内，应改用固定安排或不可用",
                        where=p.id,
                    )
                )
    # --- preferences ------------------------------------------------------
    for pref in instance.preferences:
        if pref.shift not in shifts:
            rep.issues.append(
                Issue(
                    "error",
                    "preference_unknown_shift",
                    f"偏好记录引用了不存在的班次 {pref.shift!r}",
                    where=pref.person,
                )
            )
        if pref.date is not None and pref.date not in date_set:
            rep.issues.append(
                Issue(
                    "warning",
                    "preference_outside_horizon",
                    f"{pref.person} 对 {pref.date} 的偏好落在周期之外，不产生任何影响",
                    where=pref.person,
                )
            )

    # --- shifts -----------------------------------------------------------
    for s in instance.shifts:
        if s.attendance and s.resolved_hours() <= 0:
            rep.issues.append(
                Issue("error", "shift_zero_hours", f"班次 {s.id} 的时长为 0", where=s.id)
            )
        if s.resolved_hours() > 16:
            rep.issues.append(
                Issue(
                    "warning",
                    "shift_too_long",
                    f"班次 {s.id} 时长 {s.resolved_hours()}h，超过 16 小时，请确认",
                    where=s.id,
                )
            )
    ids = [s.id for s in instance.shifts]
    if instance.rest_code in ids:
        rep.issues.append(
            Issue(
                "error",
                "rest_code_collision",
                f"休息占位符 {instance.rest_code!r} 与某个真实班次同名",
            )
        )

    # --- calendar ---------------------------------------------------------
    span = (dates[-1] - dates[0]).days + 1
    if span > 92:
        rep.issues.append(
            Issue(
                "warning",
                "long_horizon",
                f"排班周期长达 {span} 天；CP-SAT 的规模会随天数线性增长，"
                "建议按月切分并利用 history 传递跨月状态",
            )
        )
    if instance.holidays:
        stray = [h for h in instance.holidays if h not in date_set]
        if stray:
            rep.issues.append(
                Issue(
                    "info",
                    "holidays_outside_horizon",
                    f"{len(stray)} 个节假日落在周期之外，仅用于展示颜色",
                )
            )

    return rep


def lint_spec(spec: Spec) -> LintReport:
    rep = lint_instance(spec.instance)
    date_set = set(spec.instance.dates())

    # --- assignments pinned to slots that carry no demand ------------------
    # Undeclared (date, shift) pairs are forbidden by default, so a fixed assignment
    # pointing at one is almost certainly a mistake.
    for c in spec.constraints.hard:
        if getattr(c, "type", "") != "fixed_assignment" or not getattr(c, "enabled", True):
            continue
        try:
            when = dt_date_fromisoformat(str(c.date))
        except ValueError:
            continue
        shift = str(c.shift)
        if when in date_set and shift != spec.instance.rest_code \
                and spec.instance.demand_for(when, shift) is None:
            rep.issues.append(
                Issue(
                    "warning",
                    "fixed_assignment_without_demand",
                    f"固定安排 {c.person} 在 {when} 上 {shift}，但该班次当天没有需求声明",
                    hint="没有需求声明的班次默认不允许排班；请补一条 demand 规则，或确认这条固定安排是否写错",
                    where=str(c.person),
                )
            )
    if not spec.constraints.hard:
        rep.issues.append(
            Issue(
                "warning",
                "no_hard_constraints",
                "没有定义任何硬约束；排出来的表可能违反法规或学校规定",
                hint="至少应有 one_shift_per_day 与一条 coverage/requirement 类的规则",
            )
        )
    if not spec.constraints.soft:
        rep.issues.append(
            Issue(
                "info",
                "no_soft_constraints",
                "没有定义任何软约束；求解器只会找一个可行解，不会优化公平性或偏好",
            )
        )
    if not spec.assumptions and not spec.unresolved:
        rep.issues.append(
            Issue(
                "info",
                "no_assumptions_recorded",
                "规格里既没有 assumptions 也没有 unresolved；如果这是访谈产生的规格，"
                "建议把推断出的默认值显式记录下来供用户复核",
            )
        )
    # cross-check: hours bounds need shift durations
    has_hours = any(c.type == "hours_bounds" for c in spec.constraints.hard)
    if has_hours:
        unknown = [s.id for s in spec.instance.shifts if s.hours is None and not s.attendance]
        if unknown:
            rep.issues.append(
                Issue(
                    "info",
                    "hours_from_clock",
                    f"班次 {unknown} 的工时由起止时间推算得出",
                )
            )
    return rep
