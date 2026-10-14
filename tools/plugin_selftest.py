#!/usr/bin/env python3
"""End-to-end self-test for the dsh-scheduling-or plugin.

Verifies, in order, without spending a single model token:

1. the bundle is listed in the profile's ``dsh.profile.bundles``;
2. the installed bundle directory resolves and its ``cordis.patch.yml`` is readable;
3. the ``!!js`` path expressions in that patch evaluate to real paths;
4. the interpreter and repo layout those paths point at actually exist;
5. the MCP server starts **with exactly the config the bundle declares** (not a
   hand-written command) and answers ``initialize`` / ``tools/list`` / ``tools/call``.

Step 5 is the point: a hand-written client test proves the Python server works,
but only this proves the *plugin wiring* works.

    bin/sched-plugin-selftest [--profile scheduling] [--dsh-home DIR] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

BUNDLE_NAME = "dsh-scheduling-or"
MCP_ROW_ID = "mcp-schedulingor"


class JsTag:
    """Placeholder for ``!!js`` scalars so plain ``safe_load`` can read the file."""

    def __init__(self, expr: str) -> None:
        self.expr = expr

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"JsTag({self.expr!r})"


def _loader() -> type[yaml.SafeLoader]:
    class Loader(yaml.SafeLoader):
        pass

    def construct_js(loader: yaml.SafeLoader, node: yaml.Node) -> JsTag:
        return JsTag(str(loader.construct_scalar(node)))  # type: ignore[arg-type]

    Loader.add_constructor("!js", construct_js)
    Loader.add_constructor("tag:yaml.org,2002:js", construct_js)
    return Loader


def render_js(expr: str, env: dict[str, str]) -> str:
    """Evaluate a ``!!js`` expression the way the DSH loader would (via node)."""
    proc = subprocess.run(
        ["node", "-e", f"process.stdout.write(String({expr}))"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"!!js evaluation failed for {expr!r}: {proc.stderr.strip()}")
    return proc.stdout


def resolve(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, JsTag):
        return render_js(value.expr, env)
    if isinstance(value, dict):
        return {k: resolve(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, env) for v in value]
    return value


def find_mcp_row(patch: Any, row_id: str) -> dict[str, Any] | None:
    """The patch is a list of either ``{insert: [...]}`` or bare id-targeted rows."""
    for entry in patch or []:
        if not isinstance(entry, dict):
            continue
        rows = entry.get("insert")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("id") == row_id:
                    return row
        elif entry.get("id") == row_id:
            return entry
    return None


def fail(report: dict[str, Any], message: str) -> None:
    report["ok"] = False
    report["failures"].append(message)


async def call_tools(params: dict[str, Any], repo: Path, report: dict[str, Any]) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, **{k: str(v) for k, v in (params.get("env") or {}).items()}}
    server = StdioServerParameters(
        command=str(params["command"]),
        args=[str(a) for a in params.get("args", [])],
        cwd=str(params.get("cwd", repo)),
        env=env,
    )
    spec_text = (repo / "examples" / "duty-roster.spec.yaml").read_text(encoding="utf-8")

    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        report["server"] = f"{init.server_info.name} v{init.server_info.version}"

        tools = sorted(t.name for t in (await session.list_tools()).tools)
        report["tools"] = [f"mcp__{params.get('serverName','?')}__{t}" for t in tools]
        expected = {
            "get_spec_schema",
            "validate_spec",
            "lint_instance",
            "check_feasibility",
            "solve",
            "explain_infeasible",
            "get_report",
            "export_roster",
        }
        missing = expected - set(tools)
        if missing:
            fail(report, f"MCP 工具缺失: {sorted(missing)}")

        checks: dict[str, str] = {}
        bad = json.dumps({"schema_version": 1, "employees": [], "nope": 1})
        r = json.loads(
            (await session.call_tool("validate_spec", {"spec": bad})).content[0].text
        )
        checks["validate_spec(坏规格)"] = f"ok={r['ok']} stage={r['stage']}"
        if r["ok"]:
            fail(report, "validate_spec 对坏规格返回了 ok=true")

        r = json.loads(
            (await session.call_tool("validate_spec", {"spec": spec_text})).content[0].text
        )
        checks["validate_spec(好规格)"] = f"ok={r['ok']}"
        if not r["ok"]:
            fail(report, f"validate_spec 拒绝了合法规格: {r['errors'][:2]}")

        r = json.loads(
            (await session.call_tool("check_feasibility", {"spec": spec_text})).content[0].text
        )
        checks["check_feasibility"] = str(r["summary"])

        r = json.loads(
            (
                await session.call_tool(
                    "solve", {"spec": spec_text, "out_dir": "/tmp/sched_selftest"}
                )
            ).content[0].text
        )
        checks["solve"] = f"{r['status']} verified={r['verified']} 产物={len(r['artifacts'])}"
        if not r.get("feasible") or not r.get("verified"):
            fail(report, f"solve 未通过: {r.get('diagnosis') or r.get('verification_errors')}")

        r = json.loads(
            (await session.call_tool("get_report", {"run_dir": r["run_dir"]})).content[0].text
        )
        checks["get_report"] = f"ok={r.get('ok')} 公平指标={len(r.get('fairness', []))}"
        report["checks"] = checks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default=os.environ.get("SCHEDULINGOR_PROFILE", "scheduling"))
    ap.add_argument("--dsh-home", default=os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    dsh_home = Path(args.dsh_home).expanduser()
    profile_dir = dsh_home / "profiles" / args.profile
    report: dict[str, Any] = {
        "ok": True,
        "failures": [],
        "profile": args.profile,
        "profile_dir": str(profile_dir),
        "dsh_home": str(dsh_home),
    }

    # 1. profile manifest -------------------------------------------------
    manifest = profile_dir / "package.json"
    if not manifest.is_file():
        fail(report, f"profile 不存在: {profile_dir}（先跑 ./bin/sched-agent-install）")
        _emit(report, args.json)
        return 1
    data = json.loads(manifest.read_text(encoding="utf-8"))
    bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
    report["bundles"] = bundles
    if BUNDLE_NAME not in bundles:
        fail(report, f"bundle {BUNDLE_NAME!r} 不在 bundles 中: {bundles}")
        _emit(report, args.json)
        return 1

    # 2. installed bundle directory ---------------------------------------
    node_modules = profile_dir / "node_modules" / BUNDLE_NAME
    if not node_modules.exists():
        fail(report, f"bundle 目录不存在: {node_modules}")
        _emit(report, args.json)
        return 1
    report["bundle_dir"] = str(node_modules.resolve())
    patch_file = node_modules.resolve() / "cordis.patch.yml"
    if not patch_file.is_file():
        fail(report, f"bundle 缺少 cordis.patch.yml: {patch_file}")
        _emit(report, args.json)
        return 1

    # 3. patch + !!js resolution ------------------------------------------
    patch = yaml.load(patch_file.read_text(encoding="utf-8"), Loader=_loader())
    row = find_mcp_row(patch, MCP_ROW_ID)
    if row is None:
        fail(report, f"bundle patch 里找不到 id={MCP_ROW_ID!r}")
        _emit(report, args.json)
        return 1
    env = {**os.environ, "SCHEDULINGOR_HOME": os.environ.get("SCHEDULINGOR_HOME", str(repo))}
    try:
        cfg = resolve(row.get("config", {}), env)
    except Exception as exc:
        fail(report, f"!!js 解析失败: {exc}")
        _emit(report, args.json)
        return 1
    report["mcp_config"] = cfg

    # 4. do the resolved paths exist? -------------------------------------
    python_bin = Path(str(cfg.get("command", "")))
    if not python_bin.is_file():
        fail(report, f"解释器不存在: {python_bin}")
    cwd = Path(str(cfg.get("cwd", "")))
    if not (cwd / "src" / "scheduling_or" / "mcp_server.py").is_file():
        fail(report, f"cwd 下找不到内核: {cwd}")
    py_path = str((cfg.get("env") or {}).get("PYTHONPATH", ""))
    if not Path(py_path).is_dir():
        fail(report, f"PYTHONPATH 不存在: {py_path}")
    if report["failures"]:
        _emit(report, args.json)
        return 1

    # 5. talk to the server through the declared config --------------------
    try:
        asyncio.run(call_tools(cfg, repo, report))
    except Exception as exc:
        fail(report, f"MCP 交互失败: {type(exc).__name__}: {exc}")

    code = 0 if report["ok"] else 1
    _emit(report, args.json)
    return code


def _emit(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    mark = "✅" if report["ok"] else "❌"
    print(f"{mark} DSH 插件自检  profile={report['profile']}  dsh_home={report['dsh_home']}")
    print(f"   bundles      : {report.get('bundles')}")
    print(f"   bundle 目录  : {report.get('bundle_dir')}")
    cfg = report.get("mcp_config")
    if cfg:
        print(f"   解释器       : {cfg.get('command')}")
        print(f"   cwd          : {cfg.get('cwd')}")
        print(f"   PYTHONPATH   : {(cfg.get('env') or {}).get('PYTHONPATH')}")
        print(f"   failOnStartup: {cfg.get('failOnStartupError')}")
    if report.get("server"):
        print(f"   MCP server   : {report['server']}")
    for name in report.get("tools", []):
        print(f"     · {name}")
    for name, value in (report.get("checks") or {}).items():
        print(f"   {name:22s}: {value}")
    for problem in report["failures"]:
        print(f"   ✖ {problem}")


if __name__ == "__main__":
    sys.exit(main())
