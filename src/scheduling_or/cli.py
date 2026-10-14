"""Command-line interface.

Conventions that matter for automation (see docs/plans/01-方案规划.md §10):

* results go to **stdout**, progress and diagnostics to **stderr**;
* every command supports ``--json`` for machine consumption;
* exit codes carry meaning, so an agent can decide what to do next without
  parsing prose:

  ==== ==========================================================
  0    success
  2    usage error / validation failure (the user or model can fix it)
  3    proven INFEASIBLE -- a legitimate business answer, not a crash
  4    timed out without finding a feasible solution (UNKNOWN)
  ==== ==========================================================
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from . import service
from .config import AppConfig, compose, config_root
from .lint import lint_spec
from .schema.spec import Spec, validate_text

app = typer.Typer(
    name="sched",
    help="SchedulingOR — 排班/排课智能体的确定性内核。访谈产物是 YAML 规格，求解由 CP-SAT 完成。",
    no_args_is_help=True,
    add_completion=False,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INFEASIBLE = 3
EXIT_UNKNOWN = 4


def _emit(payload: Any, as_json: bool, text: str = "") -> None:
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif text:
        typer.echo(text)


def _fail(message: str, code: int = EXIT_USAGE) -> None:
    typer.secho(message, err=True, fg=typer.colors.RED)
    raise typer.Exit(code)


def _load(path: Path, as_json: bool) -> Spec:
    try:
        return service.load_spec(path)
    except FileNotFoundError:
        _fail(f"找不到规格文件：{path}")
    except Exception as exc:
        result = validate_text(path.read_text(encoding="utf-8")) if path.is_file() else {"errors": [str(exc)]}
        _emit(result, as_json, "规格校验失败：\n" + "\n".join(
            f"  - {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in result.get("errors", [])
        ))
        raise typer.Exit(EXIT_USAGE) from exc
    raise typer.Exit(EXIT_USAGE)  # pragma: no cover


def _app_config(institution: str | None, run: str | None, overrides: list[str]) -> AppConfig:
    try:
        return compose(institution=institution, run=run, overrides=overrides)
    except FileNotFoundError as exc:
        _fail(str(exc))
    except ValueError as exc:
        _fail(str(exc))
    raise typer.Exit(EXIT_USAGE)  # pragma: no cover


# --------------------------------------------------------------------------
# Discovery / diagnostics
# --------------------------------------------------------------------------
@app.command("schema")
def cmd_schema(
    out: Annotated[Path | None, typer.Option("--out", help="写入文件而不是打印")] = None,
) -> None:
    """打印规格的 JSON Schema（让模型知道它能写什么）。"""
    payload = service.tool_get_spec_schema()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"已写入 {out}")
    else:
        typer.echo(text)


@app.command("doctor")
def cmd_doctor(
    as_json: Annotated[bool, typer.Option("--json", help="机器可读输出")] = False,
) -> None:
    """环境自检：OR-Tools 版本、可选依赖、配置解析。"""
    info = service.doctor()
    _emit(info, as_json, yaml.safe_dump(info, allow_unicode=True, sort_keys=False))


@app.command("types")
def cmd_types(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """列出所有可用的约束类型。"""
    payload = service.tool_get_spec_schema()["constraint_types"]
    _emit(payload, as_json, "硬约束：\n  " + "\n  ".join(payload["hard"]) + "\n\n软约束：\n  " + "\n  ".join(payload["soft"]))


# --------------------------------------------------------------------------
# Validation chain
# --------------------------------------------------------------------------
@app.command("validate")
def cmd_validate(
    spec: Annotated[Path, typer.Argument(help="规格 YAML 路径")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """校验规格（schema + 交叉引用）。不抛异常，返回结构化错误。"""
    if not spec.is_file():
        _fail(f"找不到规格文件：{spec}")
    result = service.tool_validate(spec.read_text(encoding="utf-8"))
    if not result["ok"]:
        _emit(result, as_json, f"❌ {result['stage']} 阶段失败：\n" + "\n".join(
            f"  - {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in result["errors"]
        ))
        raise typer.Exit(EXIT_USAGE)
    _emit(result, as_json, "✅ 规格校验通过：" + json.dumps(result["summary"], ensure_ascii=False))


@app.command("lint")
def cmd_lint(
    spec: Annotated[Path, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """数据质量检查。错误会阻断后续步骤。"""
    s = _load(spec, as_json)
    rep = lint_spec(s)
    payload = {
        "ok": rep.ok,
        "summary": rep.summary(),
        "issues": [i.as_dict() for i in rep.issues],
    }
    if as_json:
        _emit(payload, True)
    else:
        for i in rep.issues:
            color = {"error": typer.colors.RED, "warning": typer.colors.YELLOW}.get(i.level)
            typer.secho(f"[{i.level}] {i.code}: {i.message}", fg=color)
            if i.hint:
                typer.echo(f"        ↳ {i.hint}")
        typer.echo(rep.summary())
    raise typer.Exit(EXIT_OK if rep.ok else EXIT_USAGE)


@app.command("precheck")
def cmd_precheck(
    spec: Annotated[Path, typer.Argument()],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """求解前的算术可行性自检（只读规格，不读 config）。比求解器快几个数量级。"""
    s = _load(spec, as_json)
    payload = service.tool_precheck(s)
    if as_json:
        _emit(payload, True)
    else:
        typer.echo(payload["summary"])
        for p in payload["problems"]:
            typer.secho(f"  ✖ [{p['code']}] {p['message']}", fg=typer.colors.RED)
        for w in payload["warnings"]:
            typer.secho(f"  ! {w}", fg=typer.colors.YELLOW)
        for sug in payload["suggestions"]:
            typer.echo(f"  ↳ {sug}")
        typer.echo(json.dumps(payload["stats"], ensure_ascii=False))
    raise typer.Exit(EXIT_OK if payload["ok"] else EXIT_INFEASIBLE)


# --------------------------------------------------------------------------
# Solve / explain / diagnose
# --------------------------------------------------------------------------
@app.command("solve")
def cmd_solve(
    spec: Annotated[Path, typer.Argument()],
    out: Annotated[Path, typer.Option("--out", "-o", help="产物目录")] = Path("runs"),
    institution: Annotated[str | None, typer.Option("--institution", "-i")] = None,
    run: Annotated[str | None, typer.Option("--run", "-r")] = None,
    set_: Annotated[list[str] | None, typer.Option("--set", "-s", help="配置覆盖 a.b=c")] = None,
    diagnose_budget: Annotated[float, typer.Option("--diagnose-budget", help="不可行诊断秒数")] = 20.0,
    skip_precheck: Annotated[bool, typer.Option("--skip-precheck")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """求解并落盘全部产物。"""
    s = _load(spec, as_json)
    cfg = _app_config(institution, run, set_ or [])
    result = service.run_solve(
        s, app=cfg, out_dir=out, diagnose_budget_s=diagnose_budget, skip_precheck=skip_precheck
    )
    sol = result.solution
    if as_json:
        _emit(result.as_dict(), True)
    else:
        if result.solver_changes or result.weight_changes:
            typer.secho(
                f"config 覆盖（config_policy={cfg.config_policy}）：", fg=typer.colors.CYAN
            )
            for ch in [*result.solver_changes, *result.weight_changes]:
                typer.secho(f"  · {ch}", fg=typer.colors.CYAN)
            typer.echo("")
        typer.echo(service.explain(s, sol))
        typer.echo("")
        typer.echo(f"产物目录：{result.run_dir}")
        for kind, path in result.artifacts.items():
            typer.echo(f"  - {kind}: {path}")
    if sol.status == "INFEASIBLE":
        raise typer.Exit(EXIT_INFEASIBLE)
    if not sol.feasible:
        raise typer.Exit(EXIT_UNKNOWN)


@app.command("diagnose")
def cmd_diagnose(
    spec: Annotated[Path, typer.Argument()],
    budget: Annotated[float, typer.Option("--budget", help="诊断预算秒数")] = 20.0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """定位互相冲突的硬约束（递进松弛法）。"""
    s = _load(spec, as_json)
    payload = service.tool_diagnose(s, budget_s=budget)
    if as_json:
        _emit(payload, True)
    else:
        typer.echo(payload["message"])
        for c in payload["conflicts"]:
            typer.secho(f"  冲突候选：{c}", fg=typer.colors.YELLOW)
        for sug in payload["suggestions"]:
            typer.echo(f"  ↳ {sug}")
    raise typer.Exit(EXIT_OK if payload["explained"] else EXIT_INFEASIBLE)


@app.command("explain")
def cmd_explain(
    run_dir: Annotated[Path, typer.Argument(help="solve 产生的产物目录")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """解释一次求解结果：达成了什么、代价多少、该改哪里。"""
    spec_path = run_dir / "resolved_spec.yaml"
    sol_path = run_dir / "solution.json"
    if not spec_path.is_file() or not sol_path.is_file():
        _fail(f"{run_dir} 里找不到 resolved_spec.yaml / solution.json")
    s = Spec.load(spec_path)
    from .schema.solution import Solution

    sol = Solution.model_validate_json(sol_path.read_text(encoding="utf-8"))
    text = service.explain(s, sol)
    _emit({"explanation": text, "status": sol.status, "verified": sol.verified}, as_json, text)


@app.command("report")
def cmd_report(
    run_dir: Annotated[Path, typer.Argument()],
) -> None:
    """打印已有产物里的 Markdown 报告。"""
    path = run_dir / "report.md"
    if not path.is_file():
        _fail(f"找不到 {path}")
    typer.echo(path.read_text(encoding="utf-8"))


@app.command("export")
def cmd_export(
    run_dir: Annotated[Path, typer.Argument()],
    fmt: Annotated[str, typer.Option("--format", "-f", help="xlsx|csv|md")] = "xlsx",
    out: Annotated[Path | None, typer.Option("--out", "-o")] = None,
) -> None:
    """从已保存的解重新导出（改表头/格式无需重解）。"""
    spec_path = run_dir / "resolved_spec.yaml"
    sol_path = run_dir / "solution.json"
    if not spec_path.is_file() or not sol_path.is_file():
        _fail(f"{run_dir} 里找不到 resolved_spec.yaml / solution.json")
    s = Spec.load(spec_path)
    from .schema.solution import Solution

    sol = Solution.model_validate_json(sol_path.read_text(encoding="utf-8"))
    target = out or run_dir
    try:
        from .io import excel_export
    except Exception as exc:
        _fail(f"导出模块不可用：{exc}")
    if fmt == "xlsx":
        path = excel_export.export_workbook(sol, s.instance, spec=s, path=Path(target) / "roster.xlsx")
        typer.echo(f"已导出 {path}")
    elif fmt == "csv":
        for p in excel_export.export_csv(sol, s.instance, out_dir=Path(target)):
            typer.echo(f"已导出 {p}")
    elif fmt == "md":
        typer.echo((run_dir / "report.md").read_text(encoding="utf-8"))
    else:
        _fail(f"不支持的格式：{fmt}")


# --------------------------------------------------------------------------
# Scaffolding / maintenance
# --------------------------------------------------------------------------
@app.command("init")
def cmd_init(
    name: Annotated[str, typer.Argument(help="机构/场景标识，例如 midterm-exam")],
    force: Annotated[bool, typer.Option("--force", help="覆盖已存在的文件")] = False,
) -> None:
    """生成一套机构配置与规格骨架。"""
    root = config_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "institutions").mkdir(exist_ok=True)
    (root / "runs").mkdir(exist_ok=True)
    inst = root / "institutions" / f"{name}.yaml"
    spec_dir = Path("specs")
    spec_dir.mkdir(exist_ok=True)
    spec_path = spec_dir / f"{name}.yaml"

    if inst.exists() and not force:
        _fail(f"{inst} 已存在；加 --force 覆盖")
    if spec_path.exists() and not force:
        _fail(f"{spec_path} 已存在；加 --force 覆盖")

    inst.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "institution": name,
                "solver": {"max_time_seconds": 60, "num_workers": 8, "strategy": "lexicographic"},
                "weights": {
                    "understaff": 1000,
                    "overstaff": 20,
                    "shift_spread": 50,
                    "weekend_spread": 50,
                    "preference": 30,
                },
                "export": {"formats": ["xlsx", "md"], "layout": "monthly"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    spec_path.write_text(_SPEC_TEMPLATE.format(name=name), encoding="utf-8")
    typer.echo(f"已生成 {inst} 与 {spec_path}")
    typer.echo("下一步：填写规格 → sched validate → sched lint → sched precheck → sched solve")


@app.command("migrate")
def cmd_migrate(
    spec: Annotated[Path, typer.Argument()],
    out: Annotated[Path | None, typer.Option("--out", "-o")] = None,
) -> None:
    """把旧版规格迁移到当前 schema_version。"""
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    version = int(data.get("schema_version", 0))
    if version == 1:
        typer.echo("已是当前版本，无需迁移。")
        return
    if version == 0:
        data["schema_version"] = 1
        target = out or spec.with_suffix(".migrated.yaml")
        target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        typer.echo(f"v0 -> v1，已写入 {target}")
        return
    _fail(f"不支持从 v{version} 迁移")


@app.command("import")
def cmd_import(
    workbook: Annotated[Path, typer.Argument(help="用户提供的 Excel")],
    institution: Annotated[str | None, typer.Option("--institution", "-i")] = None,
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("specs"),
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """按 config 里的列映射把用户表格转成 instance（暂存为 YAML 片段）。"""
    cfg = _app_config(institution, None, [])
    try:
        from .io import excel_import
    except Exception as exc:
        _fail(f"导入模块不可用：{exc}")
    from .io.excel_import import WorkbookMap

    # config declares the mapping; the io layer wants its own WorkbookMap type.
    mapping = WorkbookMap(
        staff=(cfg.excel_import.staff.model_dump() if cfg.excel_import.staff else None),
        shifts=(cfg.excel_import.shifts.model_dump() if cfg.excel_import.shifts else None),
        demand=(cfg.excel_import.demand.model_dump() if cfg.excel_import.demand else None),
        unavailability=(
            cfg.excel_import.unavailability.model_dump()
            if cfg.excel_import.unavailability
            else None
        ),
        preferences=(
            cfg.excel_import.preferences.model_dump() if cfg.excel_import.preferences else None
        ),
    )
    defaults = cfg.excel_import.defaults or {}
    try:
        result = excel_import.import_instance(
            workbook,
            mapping,
            default_role=str(defaults.get("role", "")),
            default_fte=float(defaults.get("fte", 1.0)),
            default_kind=str(defaults.get("preference_kind", "want")),  # type: ignore[arg-type]
        )
    except Exception as exc:
        _fail(f"导入失败：{type(exc).__name__}: {exc}")

    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{workbook.stem}.instance.yaml"
    parts = result.instance_parts
    target.write_text(
        yaml.safe_dump(
            parts.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )
    # The importer infers the horizon when the workbook has no 校历 sheet; surface it
    # as a first-class assumption so a human confirms it rather than trusting it.
    assumptions: list[str] = []
    if parts.horizon is not None:
        assumptions.append(
            f"排班周期由表格中的日期推断得出：{parts.horizon.start} ~ {parts.horizon.end}，请确认"
        )
    else:
        assumptions.append("表格中未找到日期信息，请补全 instance.horizon")

    payload = {
        "instance_file": str(target),
        "problems": list(result.problems),
        "assumptions": assumptions,
        "ok": result.ok,
    }
    if as_json:
        _emit(payload, True)
    else:
        typer.echo(f"已写入 {target}")
        for a in assumptions:
            typer.secho(f"  ↳ 待确认：{a}", fg=typer.colors.CYAN)
        for p in result.problems:
            typer.secho(f"  ! {p}", fg=typer.colors.YELLOW)
    if result.problems:
        raise typer.Exit(EXIT_USAGE)


_SPEC_TEMPLATE = """\
# SchedulingOR 规格骨架 — 由 `sched init {name}` 生成
# 契约定义见 src/scheduling_or/schema/，字段拼错会直接报错（extra=forbid）。
schema_version: 1
name: {name}

instance:
  horizon:
    start: 2025-09-01
    end: 2025-09-30
  shifts:
    - {{id: s1, name: 第一场, start: "09:00", end: "11:00"}}
  people: []
  groups: []
  demand: []
  unavailability: []
  preferences: []

constraints:
  hard:
    - {{type: one_shift_per_day, max_per_day: 1}}
    - {{type: max_consecutive_work_days, days: 5}}
  soft:
    - {{type: coverage_shortfall, weight: 1000}}
    - {{type: coverage_excess, weight: 20}}
    - {{type: fairness, metric: total_shifts, aggregation: spread, weight: 50}}
    - {{type: preference, weight: 30}}

solver:
  max_time_seconds: 60
  num_workers: 8
  strategy: lexicographic

# 智能体推断出来的默认值，必须逐条让用户复核
assumptions: []
# 问不出来、留待用户补充的问题
unresolved: []
"""


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
