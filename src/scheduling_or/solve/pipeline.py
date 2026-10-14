"""Solve pipeline: pre-check -> build -> (probe) -> optimise -> verify -> report.

Two strategies are supported and both are honest about what they guarantee:

``lexicographic``  group objective terms into weight-magnitude tiers, optimise the
                   heaviest tier first and lock its value before moving on. Respects
                   the priority ordering the user expressed through weights instead
                   of letting a pile of small terms outvote a big one.
``weighted_sum``   one solve over the whole weighted sum. Faster, but a large
                   number of cheap violations can outvote one expensive goal.

Nothing is reported as trustworthy until :func:`scheduling_or.verify.verify` has
independently re-checked every hard constraint.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from ..model.builder import BuiltModel, build
from ..model.modules import Ledger
from ..report import Analysis, analyse, cost_breakdown
from ..schema.solution import Assignment, Solution
from ..schema.spec import Spec
from ..verify import verify
from .diagnose import diagnose, suggestions_for
from .precheck import run_precheck


@dataclass
class SolveRequest:
    spec: Spec
    run_precheck: bool = True
    diagnose_on_infeasible: bool = True
    diagnose_budget_s: float = 20.0


@dataclass
class SolveOutcome:
    solution: Solution
    built: BuiltModel | None = None
    analysis: Analysis | None = None
    notes: list[str] = field(default_factory=list)
    #: objective value of each lexicographic tier, in optimisation order
    tier_objectives: list[float] = field(default_factory=list)


def _tiers(ledger: Ledger, max_tiers: int = 5) -> list[set[str]]:
    """Group constraint ids by objective-weight order of magnitude, heaviest first."""
    by_mag: dict[int, set[str]] = {}
    for term in ledger.terms:
        if term.weight <= 0:
            continue
        mag = math.floor(math.log10(term.weight)) if term.weight > 0 else 0
        by_mag.setdefault(mag, set()).add(term.constraint_id)
    tiers = [by_mag[m] for m in sorted(by_mag, reverse=True)]
    if len(tiers) > max_tiers:
        # merge the tail so we do not run a solve per distinct weight
        head, tail = tiers[: max_tiers - 1], tiers[max_tiers - 1 :]
        merged: set[str] = set()
        for t in tail:
            merged |= t
        tiers = [*head, merged]
    return tiers


def _extract(built: BuiltModel, solver: cp_model.CpSolver) -> list[Assignment]:
    out: list[Assignment] = []
    for p in built.vars.people:
        for d in built.vars.dates:
            for s in built.vars.shifts:
                if solver.boolean_value(built.vars.on(p, d, s)):
                    out.append(Assignment(person=p, date=d, shift=s))
    return out


def _hint_from(built: BuiltModel, solver: cp_model.CpSolver) -> None:
    for p in built.vars.people:
        for d in built.vars.dates:
            for s in [*built.vars.shifts, built.vars.rest]:
                built.model.add_hint(built.vars.on(p, d, s), int(solver.boolean_value(built.vars.on(p, d, s))))


def _configure(solver: cp_model.CpSolver, spec: Spec, *, workers: int | None = None) -> None:
    solver.parameters.random_seed = spec.solver.random_seed
    solver.parameters.num_search_workers = spec.solver.num_workers if workers is None else workers
    solver.parameters.max_time_in_seconds = spec.solver.max_time_seconds
    solver.parameters.relative_gap_limit = spec.solver.relative_gap_limit
    solver.parameters.log_search_progress = spec.solver.log_search_progress


def solve(req: SolveRequest) -> SolveOutcome:
    spec = req.spec

    # ---- stage 1: arithmetic pre-check ---------------------------------
    if req.run_precheck:
        pre = run_precheck(spec)
        if not pre.ok:
            return SolveOutcome(
                solution=Solution(
                    status="INFEASIBLE",
                    feasible=False,
                    rest_code=spec.instance.rest_code,
                    verification_errors=[],
                    notes=pre.warnings,
                    diagnosis="预检发现结构性矛盾，无需调用求解器：" + pre.summary(),
                    conflicts=[p.code for p in pre.problems],
                    suggestions=pre.suggestions(),
                ),
                notes=pre.warnings,
            )

    # ---- stage 2: build -------------------------------------------------
    built = build(spec, include_soft=True)
    notes = built.notes

    # ---- stage 3: quick feasibility probe (also seeds the hint) ----------
    probe = cp_model.CpSolver()
    _configure(probe, spec)
    probe.parameters.max_time_in_seconds = min(5.0, max(0.5, spec.solver.max_time_seconds * 0.15))
    probe_status = probe.solve(built.model)

    if probe_status == cp_model.INFEASIBLE:
        return _infeasible(req, built, notes)
    if probe_status == cp_model.MODEL_INVALID:
        return SolveOutcome(
            solution=Solution(
                status="MODEL_INVALID",
                feasible=False,
                rest_code=spec.instance.rest_code,
                diagnosis="CP-SAT 拒绝了这个模型，这通常是内核 bug，请提交 issue 并附上 spec",
                notes=notes,
            ),
            built=built,
        )
    if probe_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        _hint_from(built, probe)

    # ---- stage 4: optimise ---------------------------------------------
    solver = cp_model.CpSolver()
    _configure(solver, spec)
    started = time.monotonic()
    budget = spec.solver.max_time_seconds
    tier_objectives: list[float] = []

    tiers: list[set[str]] = []
    if spec.solver.strategy == "weighted_sum" or built.ledger.is_empty():
        if not built.ledger.is_empty():
            built.model.minimize(built.ledger.expression())
        solver.parameters.max_time_in_seconds = budget
        status = solver.solve(built.model)
    else:
        tiers = _tiers(built.ledger)
        status = probe_status
        for i, tier in enumerate(tiers):
            spent = time.monotonic() - started
            remaining = max(0.5, budget - spent)
            share = remaining / max(1, len(tiers) - i)
            solver.parameters.max_time_in_seconds = share
            expr = built.ledger.expression(only=tier)
            built.model.minimize(expr)
            status = solver.solve(built.model)
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                # keep the previous tier's solution: it is still a valid roster
                break
            tier_objectives.append(float(solver.objective_value))
            built.model.add(expr <= int(solver.objective_value))
        # fall back to the probe solution so we never return an empty roster
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE) and probe_status in (
            cp_model.OPTIMAL,
            cp_model.FEASIBLE,
        ):
            solver = probe
            status = probe_status

    wall = round(solver.wall_time, 4)
    status_name = solver.status_name(status)
    del tiers  # only used for the loop above

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if status == cp_model.INFEASIBLE:
            return _infeasible(req, built, notes)
        return SolveOutcome(
            solution=Solution(
                status="UNKNOWN",
                feasible=False,
                wall_time_s=wall,
                num_booleans=built.vars.n_booleans,
                rest_code=spec.instance.rest_code,
                notes=[
                    *notes,
                    "求解器在时限内既未找到可行解、也未证明不可行。这与 INFEASIBLE 不同："
                    "建议先延长 max_time_seconds 重试，仍不行再按「需求 → 资质 → 连续工作 → 工时」"
                    "的顺序逐类放宽硬约束。",
                ],
            ),
            built=built,
        )

    # ---- stage 5: extract + independent verification --------------------
    assignments = _extract(built, solver)
    errors = verify(spec, assignments)
    analysis = analyse(spec, assignments)
    cost = cost_breakdown(built.ledger, solver, spec)

    total_cost = float(sum(t.cost for t in cost))
    # A bound on the *total* objective is only available when there is a single
    # optimisation stage; in multi-tier mode the solver's bound covers the last tier.
    single_stage = spec.solver.strategy == "weighted_sum" or len(tier_objectives) <= 1
    solution = Solution(
        status=status_name,  # type: ignore[arg-type]
        feasible=True,
        objective=total_cost,
        best_bound=float(solver.best_objective_bound) if single_stage else None,
        strategy=spec.solver.strategy,
        tier_objectives=tier_objectives,
        wall_time_s=wall,
        num_booleans=built.vars.n_booleans,
        num_conflicts=int(solver.num_conflicts),
        branches=int(solver.num_branches),
        assignments=assignments,
        rest_code=spec.instance.rest_code,
        cost_breakdown=cost,
        fairness=analysis.fairness,
        per_person=analysis.person_stats,
        coverage=analysis.coverage,
        verified=not errors,
        verification_errors=errors,
        notes=notes,
    )
    return SolveOutcome(
        solution=solution,
        built=built,
        analysis=analysis,
        notes=notes,
        tier_objectives=tier_objectives,
    )


def _infeasible(req: SolveRequest, built: BuiltModel, notes: list[str]) -> SolveOutcome:
    spec = req.spec
    if not req.diagnose_on_infeasible:
        return SolveOutcome(
            solution=Solution(
                status="INFEASIBLE",
                feasible=False,
                rest_code=spec.instance.rest_code,
                notes=notes,
                diagnosis="已证明不可行；未启用自动诊断。",
            ),
            built=built,
        )
    diag = diagnose(spec, budget_s=req.diagnose_budget_s, seed=spec.solver.random_seed)
    return SolveOutcome(
        solution=Solution(
            status="INFEASIBLE",
            feasible=False,
            rest_code=spec.instance.rest_code,
            notes=notes,
            diagnosis=diag.message,
            conflicts=diag.conflicts,
            suggestions=suggestions_for(spec, diag.conflicts) if diag.conflicts else [],
        ),
        built=built,
        notes=notes,
    )
