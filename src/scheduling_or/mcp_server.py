"""MCP stdio server: the model-facing tool surface.

Run it directly (what ``dsh/profile.patch.yml`` does):

    .venv/bin/python -m scheduling_or.mcp_server

DSH exposes these as ``mcp__schedulingor__<name>``.

Two deliberate design choices:

* every tool **returns** structured data instead of raising, because a model can
  self-correct from ``{"ok": false, "errors": [{"loc": [...], "msg": ...}]}`` but not
  from a traceback;
* this module contains no logic of its own -- it is a thin adapter over
  :mod:`scheduling_or.service`, the same functions the CLI calls. Two
  implementations would drift, and the model would then be solving a different
  problem than the human.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import service
from .config import compose
from .schema.solution import Solution
from .schema.spec import Spec

server = MCPServer(
    name="schedulingor",
    version="0.1.0",
    instructions=(
        "排班/排课确定性内核。工作顺序固定为：get_spec_schema 了解可写字段 → 写 YAML 规格 → "
        "validate_spec 校验 → lint_instance 数据质量 → check_feasibility 预检 → solve 求解 → "
        "explain_infeasible 或 get_report 汇报 → export_roster 导出。"
        "硬约束与软约束必须分开：硬约束违反会让方案不可发布，软约束违反只付代价。"
        "绝不要自己生成求解代码，只写规格。"
    ),
)


def _err(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _load_spec(spec_text: str, fmt: str = "yaml") -> Spec | dict[str, Any]:
    result = service.tool_validate(spec_text, fmt)
    if not result["ok"]:
        return result
    try:
        return Spec.loads(spec_text, fmt)  # type: ignore[arg-type]
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
@server.tool(
    name="get_spec_schema",
    description="返回规格的 JSON Schema、全部约束类型与使用约定。写规格前必须先调用。",
)
def get_spec_schema() -> dict[str, Any]:
    return service.tool_get_spec_schema()


@server.tool(
    name="validate_spec",
    description="校验 YAML/JSON 规格。返回结构化错误（loc/type/msg）而不是抛异常，便于自行修正。",
)
def validate_spec(spec: str, fmt: str = "yaml") -> dict[str, Any]:
    return service.tool_validate(spec, fmt)


@server.tool(
    name="lint_instance",
    description="检查规格里数据的质量（重复、缺需求、人员全周期不可用等）。错误应阻断求解。",
)
def lint_instance(spec: str, fmt: str = "yaml") -> dict[str, Any]:
    loaded = _load_spec(spec, fmt)
    if isinstance(loaded, dict):
        return loaded
    rep = service.tool_lint(loaded)
    return {"ok": rep.ok, "summary": rep.summary(), "issues": [i.as_dict() for i in rep.issues]}


@server.tool(
    name="check_feasibility",
    description="算术可行性预检（不调用求解器，毫秒级）。在 solve 之前调用，可避免为显然不可行的问题浪费几分钟。",
)
def check_feasibility(spec: str, fmt: str = "yaml") -> dict[str, Any]:
    loaded = _load_spec(spec, fmt)
    if isinstance(loaded, dict):
        return loaded
    return service.tool_precheck(loaded)


@server.tool(
    name="solve",
    description="求解排班问题，返回产物目录、成本分解与校验结果。status=INFEASIBLE 时请改用 explain_infeasible。",
)
def solve(
    spec: str,
    fmt: str = "yaml",
    out_dir: str = "runs",
    institution: str = "",
    diagnose_budget_s: float = 20.0,
) -> dict[str, Any]:
    """Solve a spec.

    ``institution`` selects ``config/institutions/<name>.yaml`` (pass e.g.
    ``duty-roster``). Omit it to use only the base config, in which case the spec's
    own weights and solver parameters are authoritative.
    """
    loaded = _load_spec(spec, fmt)
    if isinstance(loaded, dict):
        return loaded
    try:
        result = service.run_solve(
            loaded,
            app=compose(institution=institution or None),
            out_dir=out_dir,
            diagnose_budget_s=diagnose_budget_s,
        )
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    payload = result.as_dict()
    payload["explanation"] = service.explain(loaded, result.solution)
    return payload


@server.tool(
    name="explain_infeasible",
    description="定位互相冲突的硬约束（递进松弛法），并给出可执行的放宽建议。",
)
def explain_infeasible(spec: str, fmt: str = "yaml", budget_s: float = 20.0) -> dict[str, Any]:
    loaded = _load_spec(spec, fmt)
    if isinstance(loaded, dict):
        return loaded
    return service.tool_diagnose(loaded, budget_s=budget_s)


@server.tool(
    name="get_report",
    description="读取一次求解的完整报告：成本分解、公平性指标、逐人工作量、覆盖缺口。",
)
def get_report(run_dir: str) -> dict[str, Any]:
    base = Path(run_dir)
    sol_path = base / "solution.json"
    if not sol_path.is_file():
        return _err(f"{sol_path} 不存在；请先调用 solve")
    sol = Solution.model_validate_json(sol_path.read_text(encoding="utf-8"))
    payload: dict[str, Any] = {
        "ok": True,
        "status": sol.status,
        "feasible": sol.feasible,
        "objective": sol.objective,
        "verified": sol.verified,
        "verification_errors": sol.verification_errors,
        "cost_breakdown": [t.model_dump() for t in sol.cost_breakdown],
        "fairness": [f.model_dump() for f in sol.fairness],
        "per_person": {k: v.model_dump() for k, v in sol.per_person.items()},
        "coverage_gaps": [
            {"date": r.date.isoformat(), "shift": r.shift, "assigned": r.assigned,
             "minimum": r.minimum, "shortfall": r.shortfall, "excess": r.excess}
            for r in sol.coverage
            if r.shortfall or r.excess
        ],
    }
    report = base / "report.md"
    if report.is_file():
        payload["markdown"] = report.read_text(encoding="utf-8")
    return payload


@server.tool(
    name="export_roster",
    description="把已求解的结果导出为用户要的格式。format: xlsx | csv | md。",
)
def export_roster(run_dir: str, format: str = "xlsx") -> dict[str, Any]:
    base = Path(run_dir)
    spec_path = base / "resolved_spec.yaml"
    sol_path = base / "solution.json"
    if not spec_path.is_file() or not sol_path.is_file():
        return _err(f"{base} 里缺少 resolved_spec.yaml 或 solution.json；请先调用 solve")
    spec = Spec.load(spec_path)
    sol = Solution.model_validate_json(sol_path.read_text(encoding="utf-8"))
    try:
        from .io import excel_export
    except Exception as exc:
        return _err(f"导出模块不可用：{exc}")
    try:
        if format == "xlsx":
            path = excel_export.export_workbook(
                sol, spec.instance, spec=spec, path=base / "roster.xlsx"
            )
            return {"ok": True, "artifacts": [str(path)]}
        if format == "csv":
            paths = excel_export.export_csv(sol, spec.instance, out_dir=base)
            return {"ok": True, "artifacts": [str(p) for p in paths]}
        if format == "md":
            return {"ok": True, "artifacts": [str(base / "report.md")]}
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")
    return _err(f"不支持的格式 {format!r}；可选 xlsx / csv / md")


@server.tool(
    name="list_constraint_types",
    description="列出全部可用的硬约束与软约束类型。不确定用哪个原语时调用。",
)
def list_constraint_types() -> dict[str, Any]:
    return service.tool_get_spec_schema()["constraint_types"]


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()


_ = json  # keep the import meaningful for future structured payloads
