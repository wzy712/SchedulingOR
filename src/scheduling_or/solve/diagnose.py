"""Infeasibility diagnosis.

Primary mechanism is **progressive relaxation**: rebuild the model with one hard
constraint disabled at a time and re-solve. It is more robust than the CP-SAT
assumptions mechanism in practice, because:

* ``sufficient_assumptions_for_infeasibility`` only minimises the core in a
  **single-threaded, non-optimisation** model -- otherwise it returns every
  assumption, which is useless as a diagnosis (verified against the official docs
  and reproduced locally);
* assumptions require every module to wrap its constraints in ``only_enforce_if``,
  and enforcement is **not supported** for automaton/table/NoOverlap constraints;
* measured at 20-person scale the assumptions run can time out and return UNKNOWN.

The arithmetic pre-check (``precheck``) runs first and catches most real cases far
more cheaply; this module is the fallback for genuine interactions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from ..model.builder import build
from ..schema.spec import Spec


@dataclass
class Diagnosis:
    explained: bool
    conflicts: list[str] = field(default_factory=list)
    inconclusive: list[str] = field(default_factory=list)
    probes: int = 0
    elapsed_s: float = 0.0
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "explained": self.explained,
            "conflicts": self.conflicts,
            "inconclusive": self.inconclusive,
            "probes": self.probes,
            "elapsed_s": round(self.elapsed_s, 3),
            "message": self.message,
        }


def _feasible(spec: Spec, *, disabled: set[str], time_limit: float, seed: int) -> str:
    """Solve with the given constraint ids disabled. Returns the CP-SAT status name."""
    original_enabled = {}
    try:
        for c in spec.constraints.hard:
            if c.id in disabled:
                original_enabled[c.id] = c.enabled
                c.enabled = False
        built = build(spec, include_soft=False)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(0.5, time_limit)
        solver.parameters.num_search_workers = 1  # deterministic, and matches the docs
        solver.parameters.random_seed = seed
        status = solver.solve(built.model)
        return solver.status_name(status)
    finally:
        for c in spec.constraints.hard:
            if c.id in original_enabled:
                c.enabled = original_enabled[c.id]


def diagnose(
    spec: Spec,
    *,
    budget_s: float = 30.0,
    seed: int = 0,
    max_probes: int = 24,
) -> Diagnosis:
    """Find hard constraints that, if relaxed, make the problem feasible."""
    started = time.monotonic()
    candidates = [c for c in spec.constraints.hard if getattr(c, "enabled", True)]
    if not candidates:
        return Diagnosis(
            explained=False,
            message="没有任何硬约束被启用，却报告不可行，请检查数据（例如未申报的不可用日期）",
            elapsed_s=time.monotonic() - started,
        )

    per_probe = max(0.5, min(5.0, budget_s / max(1, len(candidates))))
    conflicts: list[str] = []
    inconclusive: list[str] = []
    probes = 0

    for c in candidates[:max_probes]:
        if time.monotonic() - started > budget_s:
            inconclusive.extend(x.id for x in candidates[probes:] if x.id not in conflicts)
            break
        status = _feasible(spec, disabled={c.id}, time_limit=per_probe, seed=seed)
        probes += 1
        if status in ("OPTIMAL", "FEASIBLE"):
            conflicts.append(c.id)
        elif status == "UNKNOWN":
            inconclusive.append(c.id)

    elapsed = time.monotonic() - started
    if conflicts:
        message = (
            "把下列任一条硬约束放宽后即可排班，说明冲突出在它们之中（可能是单条过严，"
            "也可能是它们之间的相互作用）：" + "、".join(f"`{c}`" for c in conflicts)
        )
    elif inconclusive and not conflicts:
        message = (
            "未能在预算内定位到单条冲突约束：可能是多条约束相互作用，或问题规模导致"
            "探测超时。建议先延长时限重试，或按「需求 → 资质 → 连续工作 → 工时」的顺序"
            "逐类放宽。"
        )
    else:
        message = (
            "放宽任意单条硬约束都不能恢复可行，说明冲突来自多条约束的相互作用"
            "（例如「总需求」与「连续工作上限 + 每周休息天数」共同挤压可用人力）。"
        )

    return Diagnosis(
        explained=bool(conflicts),
        conflicts=conflicts,
        inconclusive=inconclusive,
        probes=probes,
        elapsed_s=elapsed,
        message=message,
    )


def suggestions_for(spec: Spec, conflicts: list[str]) -> list[str]:
    """Turn conflicting constraint ids into concrete, actionable advice."""
    by_id = {c.id: c for c in spec.constraints.hard}
    out: list[str] = []
    for cid in conflicts:
        c = by_id.get(cid)
        if c is None:
            continue
        t = c.type
        if t == "requirement":
            out.append(
                f"放宽 `{cid}`：把 {c.shift} 上对组 "
                f"{c.group} 的人数要求从 {c.min} 调低，"
                "或扩大该组（增加具备资质的人员）"
            )
        elif t == "max_consecutive_work_days":
            out.append(
                f"放宽 `{cid}`：把连续工作上限从 {c.days} 天上调，"
                "或增加人员以摊薄连续班次"
            )
        elif t == "weekly_rest_days":
            out.append(f"放宽 `{cid}`：把每周休息天数从 {c.min} 天调低")
        elif t == "hours_bounds":
            out.append(f"放宽 `{cid}`：调整工时上下限")
        elif t == "rest_between_shifts":
            out.append(f"放宽 `{cid}`：把最小休息从 {c.min_hours} 小时调低")
        elif t == "max_shifts_per_period":
            out.append(f"放宽 `{cid}`：把周期内班次上限从 {c.max} 调高")
        elif t == "forbid_transition":
            out.append(
                f"放宽 `{cid}`：允许 {c.from_shift} → {c.to_shift} 接续"
                "（或改为带惩罚的软约束）"
            )
        elif t == "one_shift_per_day":
            out.append(f"放宽 `{cid}`：允许一人一天排多个班次")
        elif t == "no_pair":
            out.append(f"放宽 `{cid}`：允许 {c.people} 同班")
        elif t == "pairing":
            out.append(f"放宽 `{cid}`：允许新人独立带班")
        else:
            out.append(f"放宽 `{cid}`")
    out.append("或者：增加可用人员 / 缩短排班周期 / 降低总需求人数")
    return out
