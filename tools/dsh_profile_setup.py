#!/usr/bin/env python3
"""Set up a DSH profile for SchedulingOR: create it, install the bundle, wire the model.

Idempotent: re-running changes nothing that is already correct.

    tools/dsh_profile_setup.py [--profile scheduling] [--dsh-home DIR]
                               [--provider deepseek-account|deepseek-official|none]
                               [--dry-run]

Why this is a script and not three shell commands: the provider step has to *merge*
into an existing ``cordis.patch.yml`` without destroying its comments, and the bundle
step needs a ``pnpm`` on PATH that this machine does not have globally (corepack is
shimmed from ``.tools/bin``). Both are easy to get subtly wrong by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

BUNDLE_NAME = "dsh-scheduling-or"
MODEL_ROW_ID = "agent-default-model"
TEMPLATE = "headless"

PROVIDER_BLOCK = """
# SchedulingOR 插件挂载后追加：模型走桌面版账号登录方式。
# headless 模板默认是 deepseek-official（需要 DEEPSEEK_API_KEY 环境变量）；
# 用账号登录则改为 deepseek-account，凭据由 DSH 自己从 $DSH_HOME 读取。
- id: {row_id}
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: {provider}
    model: {model}
    reasoningEffort: high
"""


class _AnyTagLoader(yaml.SafeLoader):
    """Tolerant loader: keeps `!!js` scalars as plain strings."""


def _any(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)  # type: ignore[arg-type]
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_AnyTagLoader.add_multi_constructor("", _any)


def run(cmd: list[str], *, env: dict[str, str], dry: bool) -> int:
    print(f"    $ {' '.join(cmd)}")
    if dry:
        return 0
    return subprocess.call(cmd, env=env)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default=os.environ.get("SCHEDULINGOR_PROFILE", "scheduling"))
    ap.add_argument("--dsh-home", default=os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"))
    ap.add_argument(
        "--provider",
        default="deepseek-account",
        choices=["deepseek-account", "deepseek-official", "none"],
        help="none = 不写模型覆盖，沿用 profile 模板的默认值",
    )
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    dsh_home = Path(args.dsh_home).expanduser()
    profile_dir = dsh_home / "profiles" / args.profile

    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home)
    # dsh plugin shells out to pnpm; this machine only has corepack, shimmed here.
    shim = repo / ".tools" / "bin"
    if shim.is_dir() and not shutil.which("pnpm", path=env.get("PATH", "")):
        env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
        print(f"  [i] pnpm 不在 PATH，使用垫片 {shim}")

    print(f"DSH_HOME = {dsh_home}")
    print(f"profile  = {args.profile}  ->  {profile_dir}")

    # 1. create the profile ------------------------------------------------
    if not (profile_dir / "package.json").is_file():
        print(f"[1/3] 创建 profile（模板 {TEMPLATE}）")
        code = run(
            ["dsh", "--profile", args.profile, "--from-default-profile", TEMPLATE],
            env=env,
            dry=args.dry_run,
        )
        if code != 0 and not args.dry_run:
            print(f"  ✖ 创建 profile 失败（exit {code}）", file=sys.stderr)
            return code
    else:
        print("[1/3] profile 已存在")

    # 2. install the bundle ------------------------------------------------
    manifest = json.loads((profile_dir / "package.json").read_text(encoding="utf-8"))
    bundles = manifest.get("dsh", {}).get("profile", {}).get("bundles", [])
    print(f"[2/3] 安装 bundle（当前 bundles: {bundles}）")
    code = run(
        ["dsh", "plugin", "--profile", args.profile, "add", str(repo / "dsh-plugin")],
        env=env,
        dry=args.dry_run,
    )
    if code != 0 and not args.dry_run:
        print(f"  ✖ 安装 bundle 失败（exit {code}）", file=sys.stderr)
        return code

    # 3. model provider ----------------------------------------------------
    patch_file = profile_dir / "cordis.patch.yml"
    if args.provider == "none":
        print("[3/3] 跳过模型 provider 覆盖（--provider none）")
    elif not patch_file.is_file():
        print(f"[3/3] ✖ 找不到 {patch_file}", file=sys.stderr)
        return 1
    else:
        text = patch_file.read_text(encoding="utf-8")
        try:
            parsed = yaml.load(text, Loader=_AnyTagLoader) or []
        except yaml.YAMLError as exc:
            print(f"[3/3] ✖ 无法解析 {patch_file}: {exc}", file=sys.stderr)
            return 1
        if isinstance(parsed, list) and any(
            isinstance(row, dict) and row.get("id") == MODEL_ROW_ID for row in parsed
        ):
            print(f"[3/3] 模型 provider 已配置（{MODEL_ROW_ID} 已存在），保持不动")
        else:
            print(f"[3/3] 写入模型 provider: {args.provider}")
            if not args.dry_run:
                # Keep the generated header comments; drop a bare `[]` document.
                kept = [
                    line
                    for line in text.splitlines()
                    if line.strip() not in ("[]",)
                ]
                body = "\n".join(kept).rstrip() + "\n"
                body += PROVIDER_BLOCK.format(
                    row_id=MODEL_ROW_ID, provider=args.provider, model=args.model
                )
                patch_file.write_text(body, encoding="utf-8")

    print()
    print("完成。接下来：")
    print(f"  {repo}/bin/sched-plugin-selftest --profile {args.profile}")
    print(f'  {repo}/bin/sched-agent "帮我排 9 月的教师值班表"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
