"""Use-case layer.

Both the human CLI and the MCP tool surface call **these** functions and nothing
else. Keeping one implementation is what stops the two interfaces from drifting:
the model-driven path and the human path cannot disagree about what "solve" means.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig, apply_solver_params, apply_weights, compose, describe
from .lint import LintReport, lint_spec
from .model.modules import known_types
from .report import render_markdown
from .schema.solution import Solution
from .schema.spec import Spec, validate_text
from .solve import run_precheck, solve
from .solve.diagnose import diagnose, suggestions_for
from .solve.pipeline import SolveRequest


# --------------------------------------------------------------------------
# Read-only operations
# --------------------------------------------------------------------------
def load_spec(path: str | Path) -> Spec:
    return Spec.load(path)


def tool_get_spec_schema() -> dict[str, Any]:
    """JSON Schema of a spec, so a model can see exactly what it may write."""
    return {
        "spec_schema": Spec.model_json_schema(),
        "constraint_types": known_types(),
        "schema_version": 1,
        "notes": [
            "字段拼错会直接报错（extra=forbid），这是有意的：静默忽略一个拼错的约束是最危险的失败模式。",
            "约束分为 hard（违反即不可发布）与 soft（违反要付代价并会计入报告）两类，"
            "不要把硬约束写成大 weight 的软约束。",
            "assumptions 用来记录你推断出来的默认值，unresolved 用来记录问不出来的问题；两者都必须显式填写。",
        ],
    }


def tool_validate(spec_text: str, fmt: str = "yaml") -> dict[str, Any]:
    return validate_text(spec_text, fmt)  # type: ignore[arg-type]


def tool_lint(spec: Spec) -> LintReport:
    return lint_spec(spec)


def tool_precheck(spec: Spec) -> dict[str, Any]:
    pre = run_precheck(spec)
    return {
        "ok": pre.ok,
        "summary": pre.summary(),
        "problems": [p.as_dict() for p in pre.problems],
        "warnings": pre.warnings,
        "suggestions": pre.suggestions(),
        "stats": pre.stats,
    }


def tool_diagnose(spec: Spec, budget_s: float = 20.0) -> dict[str, Any]:
    diag = diagnose(spec, budget_s=budget_s, seed=spec.solver.random_seed)
    out = diag.as_dict()
    out["suggestions"] = suggestions_for(spec, diag.conflicts)
    return out


# --------------------------------------------------------------------------
# Solve + persist
# --------------------------------------------------------------------------
@dataclass
class RunResult:
    solution: Solution
    spec: Spec
    run_dir: Path | None = None
    artifacts: dict[str, Path] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: config weight overrides that were actually applied (id: old -> new)
    weight_changes: list[str] = field(default_factory=list)
    #: config solver-parameter overrides that were actually applied
    solver_changes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.solution.status,
            "feasible": self.solution.feasible,
            "objective": self.solution.objective,
            "best_bound": self.solution.best_bound,
            "wall_time_s": self.solution.wall_time_s,
            "verified": self.solution.verified,
            "verification_errors": self.solution.verification_errors,
            "diagnosis": self.solution.diagnosis,
            "conflicts": self.solution.conflicts,
            "suggestions": self.solution.suggestions,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "artifacts": {k: str(v) for k, v in self.artifacts.items()},
            "assignments": len(self.solution.assignments),
            "cost_breakdown": [t.model_dump() for t in self.solution.cost_breakdown],
            "weight_changes": self.weight_changes,
            "solver_changes": self.solver_changes,
        }


def new_run_dir(base: str | Path, name: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or "run"))
    safe = "-".join(part for part in safe.split("-") if part)[:48] or "run"
    path = Path(base) / f"{stamp}-{safe}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_solve(
    spec: Spec,
    *,
    app: AppConfig | None = None,
    out_dir: str | Path = "runs",
    diagnose_budget_s: float = 20.0,
    skip_precheck: bool = False,
) -> RunResult:
    """Solve, verify, persist the resolved inputs and every artifact."""
    app = app or compose()

    # Config is the tuning surface: push weights AND solver parameters into the spec
    # *before* the evidence is dumped, so resolved_spec.yaml is exactly what was
    # solved. (Both sections were previously read and then ignored.)
    weight_changes = apply_weights(spec, app)
    solver_changes = apply_solver_params(spec, app)
    run_dir = new_run_dir(out_dir, spec.name or "run")

    # Reproduction evidence: the *merged* inputs, not the originals.
    spec.dump(run_dir / "resolved_spec.yaml")
    import yaml as _yaml

    (run_dir / "resolved_config.yaml").write_text(
        _yaml.safe_dump(app.model_dump(mode="json", by_alias=True), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    outcome = solve(
        SolveRequest(
            spec=spec,
            run_precheck=not skip_precheck,
            diagnose_on_infeasible=True,
            diagnose_budget_s=diagnose_budget_s,
        )
    )
    sol = outcome.solution

    artifacts: dict[str, Path] = {}
    if weight_changes or solver_changes:
        sol.notes = [
            *sol.notes,
            f"以下参数由 config 覆盖（config_policy={app.config_policy}）："
            + "；".join([*solver_changes, *weight_changes]),
        ]
        (run_dir / "applied_config.json").write_text(
            json.dumps(
                {
                    "policy": app.config_policy,
                    "solver_changes": solver_changes,
                    "weight_changes": weight_changes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts["applied_config"] = run_dir / "applied_config.json"
    (run_dir / "solution.json").write_text(
        sol.model_dump_json(indent=2), encoding="utf-8"
    )
    artifacts["solution"] = run_dir / "solution.json"

    md = render_markdown(
        spec,
        status=sol.status,
        objective=sol.objective,
        best_bound=sol.best_bound,
        strategy=sol.strategy,
        wall_time_s=sol.wall_time_s,
        cost=sol.cost_breakdown,
        analysis=outcome.analysis,
        verified=sol.verified,
        verification_errors=sol.verification_errors,
        notes=sol.notes,
        diagnosis=sol.diagnosis,
        conflicts=sol.conflicts,
        suggestions=sol.suggestions,
    ) if outcome.analysis is not None else _minimal_markdown(spec, sol)
    (run_dir / "report.md").write_text(md, encoding="utf-8")
    artifacts["report"] = run_dir / "report.md"

    if sol.feasible:
        try:
            from .io import excel_export  # imported lazily: keeps the kernel usable without it

            written = excel_export.export_workbook(
                sol, spec.instance, spec=spec, path=run_dir / "roster.xlsx", layout=app.export.layout
            )
            artifacts["roster_xlsx"] = Path(written)
            for extra in app.export.formats:
                if extra == "csv":
                    for p in excel_export.export_csv(sol, spec.instance, out_dir=run_dir):
                        artifacts[f"roster_{Path(p).suffix.lstrip('.')}"] = Path(p)
                elif extra == "md":
                    artifacts["report_md"] = run_dir / "report.md"
                elif extra == "json":
                    artifacts["solution_json"] = run_dir / "solution.json"
        except Exception as exc:
            sol.notes.append(f"导出 Excel 失败：{type(exc).__name__}: {exc}")

    return RunResult(
        solution=sol,
        spec=spec,
        run_dir=run_dir,
        artifacts=artifacts,
        notes=sol.notes,
        weight_changes=weight_changes,
        solver_changes=solver_changes,
    )


def _minimal_markdown(spec: Spec, sol: Solution) -> str:
    lines = [f"# 排班结果 — {spec.name or '(未命名)'}", "", f"- 状态：**{sol.status}**"]
    if sol.diagnosis:
        lines += ["", "## 诊断", "", sol.diagnosis]
    for c in sol.conflicts:
        lines.append(f"- 冲突约束：`{c}`")
    for s in sol.suggestions:
        lines.append(f"- 建议：{s}")
    for n in sol.notes:
        lines.append(f"- 提示：{n}")
    return "\n".join(lines) + "\n"


def explain(spec: Spec, sol: Solution) -> str:
    """Human-readable narrative: what was achieved, what it cost, what to change."""
    if not sol.feasible:
        parts = [f"未能排出可行方案（状态 {sol.status}）。"]
        if sol.diagnosis:
            parts.append(sol.diagnosis)
        if sol.conflicts:
            parts.append("相关约束：" + "、".join(f"`{c}`" for c in sol.conflicts))
        for s in sol.suggestions:
            parts.append(f"- {s}")
        return "\n".join(parts)

    total = sum(t.cost for t in sol.cost_breakdown)
    lines = [
        f"已排出一份{'已证明最优' if sol.status == 'OPTIMAL' else '当前最优可行'}的方案，"
        f"总代价 {total:.0f}。",
        "硬约束已由独立校验器逐条复核：" + ("全部通过。" if sol.verified else "**存在违反，见下表**"),
        "",
        "代价构成（从大到小）：",
    ]
    for t in sol.cost_breakdown:
        if t.cost == 0:
            continue
        lines.append(f"- {t.constraint_id}：违反 {t.violations} 次 × 权重 {t.weight} = {t.cost}"
                     f"（占 {t.share * 100:.1f}%）")
    if all(t.cost == 0 for t in sol.cost_breakdown):
        lines.append("- 所有软约束都被完全满足，没有任何代价。")

    if sol.fairness:
        worst = max(sol.fairness, key=lambda f: f.spread)
        if worst.spread:
            lines.append(
                f"\n公平性上波动最大的是「{worst.metric}」，极差 {worst.spread}"
                f"（最少 {worst.minimum}，最多 {worst.maximum}）。"
            )
        else:
            lines.append("\n各项公平性指标的极差都是 0，分配完全均衡。")

    short = sum(r.shortfall for r in sol.coverage)
    over = sum(r.excess for r in sol.coverage)
    lines.append(f"\n覆盖情况：缺口 {short} 人班，超配 {over} 人班。")

    if not sol.verified:
        lines.append("\n⚠️ 校验失败明细：")
        for e in sol.verification_errors[:20]:
            lines.append(f"- {e}")
    return "\n".join(lines)


def doctor() -> dict[str, Any]:
    """Environment self-check: versions, optional extras, profile wiring."""
    import ortools

    from .model.modules import known_types as _kt

    info: dict[str, Any] = {
        "ortools": getattr(ortools, "__version__", "unknown"),
        "constraint_types": _kt(),
        "config": describe(compose()),
        "config_root": str(compose.__module__ and __import__("scheduling_or.config", fromlist=["config_root"]).config_root()),
    }
    try:
        import mcp

        info["mcp"] = getattr(mcp, "__version__", "installed")
    except Exception:
        info["mcp"] = "not installed (MCP server unavailable)"
    try:
        import openpyxl
        import xlsxwriter

        info["openpyxl"] = openpyxl.__version__
        info["xlsxwriter"] = xlsxwriter.__version__
    except Exception:
        info["excel"] = "missing (export unavailable)"
    return info


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def copy_into(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.copy2(src, dst)
    return dst
