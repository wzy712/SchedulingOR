"""Post-solve analytics: cost breakdown, fairness metrics, per-person stats, coverage.

Everything here is derived from the **assignments**, not from the solver, so the
same numbers can be recomputed from a hand-edited roster. That property is what
makes the report trustworthy: it is a measurement, not a restatement of the model.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .model.modules import Ledger
from .schema.instance import Instance
from .schema.solution import (
    Assignment,
    CoverageRow,
    FairnessMetric,
    ObjectiveTerm,
    PersonStats,
)
from .schema.spec import Spec


@dataclass
class Analysis:
    person_stats: dict[str, PersonStats] = field(default_factory=dict)
    coverage: list[CoverageRow] = field(default_factory=list)
    fairness: list[FairnessMetric] = field(default_factory=list)


def _gini(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    total = sum(values)
    if total == 0:
        return 0.0
    xs = sorted(values)
    n = len(xs)
    cumulative = sum((i + 1) * x for i, x in enumerate(xs))
    return round((2 * cumulative) / (n * total) - (n + 1) / n, 6)


def _stddev(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return round(var**0.5, 4)


def _metric_values(
    instance: Instance, assignments: list[Assignment], metric: str
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    if metric == "total_shifts":
        for a in assignments:
            if a.shift != instance.rest_code:
                counts[a.person] += 1
    elif metric == "weekend_shifts":
        for a in assignments:
            if a.shift != instance.rest_code and a.date.weekday() >= 5:
                counts[a.person] += 1
    elif metric == "holiday_shifts":
        holidays = set(instance.holidays)
        for a in assignments:
            if a.shift != instance.rest_code and a.date in holidays:
                counts[a.person] += 1
    elif metric.startswith("shift:"):
        sid = metric.split(":", 1)[1]
        for a in assignments:
            if a.shift == sid:
                counts[a.person] += 1
    return {p.id: counts.get(p.id, 0) for p in instance.people}


def _summarise(metric: str, values: dict[str, int]) -> FairnessMetric:
    xs = list(values.values())
    return FairnessMetric(
        metric=metric,
        per_person=dict(values),
        minimum=min(xs) if xs else 0,
        maximum=max(xs) if xs else 0,
        mean=round(sum(xs) / len(xs), 4) if xs else 0.0,
        spread=(max(xs) - min(xs)) if xs else 0,
        stddev=_stddev(xs),
        gini=_gini(xs),
    )


def analyse(spec: Spec, assignments: list[Assignment]) -> Analysis:
    instance = spec.instance
    dates = instance.dates()
    shifts = instance.shift_ids

    # ---- per person ------------------------------------------------------
    stats: dict[str, PersonStats] = {p.id: PersonStats() for p in instance.people}
    for a in assignments:
        if a.shift == instance.rest_code:
            continue
        s = stats.setdefault(a.person, PersonStats())
        s.shifts += 1
        s.by_shift[a.shift] = s.by_shift.get(a.shift, 0) + 1
        if a.date.weekday() >= 5:
            s.weekend_shifts += 1
        with contextlib.suppress(KeyError):
            s.hours = round(s.hours + instance.shift(a.shift).resolved_hours(), 4)

    assigned: dict[tuple[str, dt.date], set[str]] = {}
    for a in assignments:
        if a.shift != instance.rest_code:
            assigned.setdefault((a.person, a.date), set()).add(a.shift)

    for pref in instance.preferences:
        if pref.person not in stats or pref.shift not in shifts:
            continue
        for d in dates:
            if not pref.matches(d):
                continue
            st = stats[pref.person]
            st.preferences_requested += 1
            hit = pref.shift in assigned.get((pref.person, d), set())
            if (pref.kind == "want" and hit) or (pref.kind == "avoid" and not hit):
                st.preferences_met += 1

    # ---- coverage --------------------------------------------------------
    coverage: list[CoverageRow] = []
    for d in dates:
        for s in shifts:
            rule = instance.demand_for(d, s)
            if rule is None:
                continue
            n = sum(1 for p in instance.person_ids if s in assigned.get((p, d), set()))
            coverage.append(
                CoverageRow(
                    date=d,
                    shift=s,
                    minimum=rule.min,
                    target=rule.target,
                    maximum=rule.max,
                    assigned=n,
                )
            )

    # ---- fairness --------------------------------------------------------
    metrics = {c.metric for c in spec.constraints.soft if getattr(c, "type", "") == "fairness"}
    metrics |= {"total_shifts", "weekend_shifts", "holiday_shifts"}
    metrics |= {f"shift:{s}" for s in shifts}
    fairness = [_summarise(m, _metric_values(instance, assignments, m)) for m in sorted(metrics)]

    return Analysis(person_stats=stats, coverage=coverage, fairness=fairness)


def cost_breakdown(
    ledger: Ledger, solver: cp_model.CpSolver, spec: Spec
) -> list[ObjectiveTerm]:
    """Per-constraint-family weighted cost, with each term's share of the total."""
    labels: dict[str, str] = {}
    for c in spec.constraints.soft:
        labels[c.id] = str(getattr(c, "note", "") or c.type)

    grouped: dict[tuple[str, str], ObjectiveTerm] = {}
    weights_seen: dict[tuple[str, str], set[int]] = {}
    for term in ledger.terms:
        key = (term.constraint_id, term.family)
        entry = grouped.get(key)
        if entry is None:
            entry = ObjectiveTerm(
                constraint_id=term.constraint_id,
                family=term.family,
                label=labels.get(term.constraint_id, term.family),
                weight=term.weight,
            )
            grouped[key] = entry
        weights_seen.setdefault(key, set()).add(term.weight)
        v = term.violation(solver)
        entry.violations += v
        entry.cost += v * term.weight

    total = sum(e.cost for e in grouped.values()) or 1
    out = sorted(grouped.values(), key=lambda e: -e.cost)
    for e in out:
        e.share = round(e.cost / total, 4)
        key = (e.constraint_id, e.family)
        if len(weights_seen.get(key, ())) > 1:
            # e.g. `preference` carries one term per request, each weighted by the
            # requester's own priority. Reporting the first term's weight as if it were
            # the whole family's is simply wrong; report the effective average.
            e.weight = round(e.cost / e.violations) if e.violations else 0
            e.label = (e.label + " (权重因条目而异，此处为加权平均)").strip()
    return out


def render_markdown(
    spec: Spec,
    *,
    status: str,
    objective: float | None,
    best_bound: float | None = None,
    strategy: str = "",
    wall_time_s: float,
    cost: list[ObjectiveTerm],
    analysis: Analysis,
    verified: bool,
    verification_errors: list[str],
    notes: list[str],
    diagnosis: str | None = None,
    conflicts: list[str] | None = None,
    suggestions: list[str] | None = None,
) -> str:
    total = sum(t.cost for t in cost) or 1
    lines: list[str] = []
    lines.append(f"# 排班结果 — {spec.name or '(未命名)'}\n")
    lines.append(f"- 求解状态：**{status}**")
    if objective is not None:
        lines.append(f"- 总加权代价：**{objective:.0f}**")
    if best_bound is not None:
        lines.append(f"- 该目标的界：{best_bound:.0f}")
    elif strategy == "lexicographic":
        lines.append(
            "- 词典序分层求解：以下代价按生效权重求和；求解器给出的界只针对最后一层，"
            "因此不在此报告为总代价的界"
        )
    lines.append(f"- 用时：{wall_time_s:.2f}s")
    lines.append(
        f"- 硬约束复核：{'✅ 全部通过（独立校验器）' if verified else '❌ 存在违反'}"
    )
    if spec.assumptions:
        lines.append(f"- 智能体推断（待复核）：{len(spec.assumptions)} 条")
    if spec.unresolved:
        lines.append(f"- 未决问题：{len(spec.unresolved)} 条")
    lines.append("")

    if diagnosis:
        lines.append("## 排不出来：冲突诊断\n")
        lines.append(diagnosis + "\n")
        if conflicts:
            lines.append("**冲突的约束：**\n")
            for c in conflicts:
                lines.append(f"- `{c}`")
            lines.append("")
        if suggestions:
            lines.append("**建议：**\n")
            for s in suggestions:
                lines.append(f"- {s}")
            lines.append("")

    if not verified and verification_errors:
        lines.append("## ⚠️ 校验失败明细\n")
        for e in verification_errors[:50]:
            lines.append(f"- {e}")
        lines.append("")

    lines.append("## 软约束代价\n")
    lines.append("| 软约束 | 违反量 | 权重 | 加权代价 | 占比 |")
    lines.append("|---|---:|---:|---:|---:|")
    for t in cost:
        lines.append(
            f"| {t.constraint_id} | {t.violations} | {t.weight} | {t.cost} "
            f"| {t.cost / total * 100:.1f}% |"
        )
    lines.append(
        f"| **合计（即总加权代价）** | | | **{sum(t.cost for t in cost)}** | 100% |"
    )
    lines.append("")

    lines.append("## 公平性指标\n")
    lines.append("| 指标 | 最少 | 最多 | 极差 | 均值 | 标准差 | 基尼 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for f in analysis.fairness:
        lines.append(
            f"| {f.metric} | {f.minimum} | {f.maximum} | {f.spread} | {f.mean} "
            f"| {f.stddev} | {f.gini} |"
        )
    lines.append("")

    lines.append("## 个人工作量\n")
    lines.append("| 人员 | 班次数 | 工时 | 周末班 | 偏好满足率 |")
    lines.append("|---|---:|---:|---:|---:|")
    for pid, st in analysis.person_stats.items():
        rate = st.preference_rate
        lines.append(
            f"| {pid} | {st.shifts} | {st.hours:.1f} | {st.weekend_shifts} "
            f"| {'—' if rate is None else f'{rate * 100:.0f}%'} |"
        )
    lines.append("")

    shortfall = sum(r.shortfall for r in analysis.coverage)
    excess = sum(r.excess for r in analysis.coverage)
    lines.append("## 覆盖情况\n")
    lines.append(f"- 覆盖槽位：{len(analysis.coverage)}")
    lines.append(f"- 缺口合计：{shortfall}")
    lines.append(f"- 超配合计：{excess}")
    lines.append("")

    if notes:
        lines.append("## 建模提示\n")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")

    return "\n".join(lines)
