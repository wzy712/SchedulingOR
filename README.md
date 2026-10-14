# SchedulingOR

基于 **DSH（DeepSeek Harness）二次开发 + Google OR-Tools CP-SAT** 的排班/排课智能体。

把口头的排班需求固化成**可校验的 YAML 规格** → CP-SAT 求最优/次优解 → **独立校验器**逐条复核
→ 导出用户要的表格。所有评估参数落在配置文件里，随时可改，不需要动代码。

适用于**考试监考、教师值班/值周、课后服务/晚自习辅导**等教务场景，内核是领域中立的。

本仓库只包含**运行所需的最小文件集**：DSH 插件、它挂载的 Python 内核、配套技能与示例。

---

## 它由三部分组成

| 部分 | 路径 | 作用 |
|---|---|---|
| **DSH 插件** | `dsh-plugin/` | 把 Python 内核挂成 9 个 MCP 工具 `mcp__schedulingor__*`。纯配置型 bundle，无需构建 |
| **确定性内核** | `src/scheduling_or/` | schema 契约 / 约束模块库 / CP-SAT 求解 / 独立校验器 / Excel 导入导出 / CLI |
| **智能体技能** | `.dsh/skills/scheduling-interview/` | 访谈流程：问什么、什么顺序、何时该停下来问用户 |

**内核不依赖 DSH、不依赖大模型**：`./bin/sched` 可以完全独立地排班。

---

## 环境要求

- Python ≥ 3.11（实测 3.14.6）
- [uv](https://docs.astral.sh/uv/)（或任何能装 `pyproject.toml` 依赖的工具）
- 只有用 DSH 插件时才需要：Node ≥ 24 + `dsh` CLI

## 安装

```bash
git clone https://github.com/wzy712/SchedulingOR.git
cd SchedulingOR

# 依赖。若你的环境不允许写 ~/.cache/uv，必须把 uv 缓存指定到仓库内
UV_CACHE_DIR=.uv-cache VIRTUAL_ENV=.venv uv pip install -e .
```

> 若安装后 `import scheduling_or` 仍失败，见「排障」第一条。

### 安装为 DSH 插件

```bash
./bin/sched-agent-install
```

它会：创建 `headless` 模板的 DSH profile → 安装 `dsh-plugin` bundle → 写入模型 provider。
幂等，可重复执行；`--dry-run` 先看要做什么，`--provider none` 跳过模型配置。

## 使用

### A. 直接用 CLI（不需要 DSH）

**一律用 `./bin/sched`**——它显式设置 `PYTHONPATH`，不依赖 venv 的安装状态。

```bash
./bin/sched --help

# 用现成场景模板
./bin/sched precheck examples/duty-roster.spec.yaml   # 毫秒级可行性自检
./bin/sched solve    examples/duty-roster.spec.yaml -i duty-roster
./bin/sched explain  runs/<时间戳>-教师值班表-合成样例   # 人话解释
./bin/sched export   runs/<...> -f csv                # 换格式重导，不重解

# 从零建一个场景
./bin/sched init my-scenario          # 生成 specs/my-scenario.yaml 与机构配置骨架
# 编辑该文件后：
./bin/sched validate specs/my-scenario.yaml
./bin/sched lint     specs/my-scenario.yaml
./bin/sched precheck specs/my-scenario.yaml
./bin/sched solve    specs/my-scenario.yaml -i my-scenario
```

`-i` 只被 `solve` 与 `import` 接受；`diagnose` 收的是**规格**而不是 run 目录。

### B. 作为 DSH 智能体

```bash
./bin/sched-agent "帮我排 9 月的教师值班表，张老师周三不能值班"
```

`bin/sched-agent` 会设好 `SCHEDULINGOR_HOME` 并调用 `dsh --profile scheduling`。

想在本机 Web 界面里用：从侧边栏 **Plugins** 页面安装，目标填本仓库的 `dsh-plugin/` 目录。

### C. 验证插件是否装好（不消耗模型额度）

```bash
./bin/sched-plugin-selftest
```

它按 bundle **实际声明的配置**去启动 MCP，验证：bundle 在 `dsh.profile.bundles` 里 →
bundle 目录可解析 → `cordis.patch.yml` 的 `!!js` 路径表达式求值正确 → 解释器与内核存在 →
`initialize` / `tools/list` / `validate_spec` / `check_feasibility` / `solve` / `get_report` 全通。
失败时退出码 1，`--json` 供 CI 使用。

---

## 三套场景模板

| 场景 | 规格文件 | 核心约束 |
|---|---|---|
| 期末考试监考 | `examples/exam-invigilation.spec.yaml` | 回避本科目、一天最多两场、人均场次均衡 |
| 教师值班/值周 | `examples/duty-roster.spec.yaml` | 周末与夜班均衡、每周休息、12h 最小休息 |
| 课后服务/晚自习 | `examples/after-school.spec.yaml` | 按科目匹配、资深带班、课时均衡 |

它们是**生成的合成数据**，用 `python examples/generate_samples.py` 可重新生成。
换成真实数据时只需按你的表头写列映射（`config/institutions/*.yaml` 的 `import` 段），
跑 `./bin/sched import`，不用改代码。

---

## 约束原语

**硬约束**（违反即不可发布）：
`one_shift_per_day` · `rest_between_shifts` · `max_consecutive_work_days` ·
`min_consecutive_work_days` · `forbid_transition` · `weekly_rest_days` ·
`max_shifts_per_period` · `hours_bounds` · `requirement` · `fixed_assignment` ·
`no_pair` · `pairing`

**软约束**（违反要付代价，计入报告）：
`coverage_shortfall` · `coverage_excess` · `skill_shortfall` · `fairness`
（`total_shifts` / `weekend_shifts` / `holiday_shifts` / `shift:<id>`）· `preference` ·
`consecutive_work_preference` · `penalized_transition` · `target_shifts_deviation`

```bash
./bin/sched types     # 列出全部
./bin/sched schema    # 打印规格的 JSON Schema
```

### 三条容易踩的语义

1. **没有声明 `demand` 的「日期 × 班次」= 不允许排班**。这是保护机制：早期版本里它完全不受约束，
   求解器会凭空给人排班且毫无报错。要表达"可排可不排"，写 `min: 0, target: 0, max: N`。
2. **`min` / `target` / `max` 语义不同**：`min` 是软下限（重罚），`target` 是理想值，
   **`max` 是硬上限**。`max` 之所以是硬的——「极差最小化」的公平目标在软上限下会诱导求解器
   往同一班次堆人来抹平极差。
3. **`holidays` 默认不改变排班**，只用于配色。要停排需显式设
   `weekday_demand_applies_on_holidays: false`；`lint` 会把这个选择报出来。

### 组（Group）是一等公民

约束只引用组名，不引用人员清单，人员变动时只改组定义：

```yaml
instance:
  groups:
    - { id: icu_qualified, members: { by_skill: icu } }
    - { id: icu_non_junior, members: { intersect: [icu_qualified, "not:juniors"] } }
constraints:
  hard:
    - { type: requirement, shift: night, group: icu_qualified, min: 1 }
```

---

## 配置如何影响求解

优先级：`config/base.yaml` → `config/institutions/<name>.yaml` → `--set a.b=c`（后者覆盖前者）。
由 `config_policy` 决定配置与规格的强弱关系：

| `config_policy` | 含义 | 何时生效 |
|---|---|---|
| `fill_missing`（默认） | 规格里写了的以规格为准，配置只补规格没写的 | 不选机构、不传 `--set` |
| `override` | 配置直接覆盖规格 | **选 `-i <机构>` 或传 `--set` 时自动启用** |
| `ignore` | 完全按规格 | 复现旧结果 |

```bash
# 调权重（传 --set 会自动进入 override）
./bin/sched solve specs/x.yaml -i midterm -s weights.understaff=0

# 加时间盒
./bin/sched solve specs/x.yaml -i midterm -s solver.max_time_seconds=300
```

`solve` 会把每一处覆盖打印出来，并写入 `runs/<...>/applied_config.json`，所以调参不会"悄悄不生效"。
`weight: 0` = **停用**该软约束，便于 A/B 对比。

---

## 退出码

| 码 | 含义 | 该做什么 |
|---|---|---|
| 0 | 成功 | 继续 |
| 1 | 内部错误 | 原文上报，不要重试 |
| 2 | 校验失败 / 用法错误 | 修规格后重试 |
| 3 | **已证明不可行** | 跑 `diagnose`，向用户解释冲突；不要重试 |
| 4 | 超时未找到可行解（UNKNOWN） | 加时间盒；**不要**当成不可行 |

结果走 stdout，进度走 stderr；能加 `--json` 就加。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'scheduling_or'` | venv 的 `.pth` 被 macOS 打上 `UF_HIDDEN`，CPython 3.14 静默跳过 | 用 `./bin/sched`；或 `chflags nohidden .venv/lib/python*/site-packages/*.pth` |
| `uv pip install` 报 `Failed to initialize cache at ~/.cache/uv` | 环境不允许写用户缓存 | 加 `UV_CACHE_DIR=.uv-cache` |
| 规格校验报 `extra_forbidden` | 字段拼错（**这是有意的**，静默忽略才危险） | 按报错改；用 `./bin/sched types` 查可用约束 |
| `attributes` 值校验失败 | YAML 把裸写的 `yes/no/on/off` 解析成布尔 | 加引号：`party: "yes"` |
| `dsh plugin` 报 `pnpm was not found` | 只有 corepack | `./bin/sched-agent-install` 会自动用仓库内 `.tools/bin/pnpm` 垫片 |
| 求解 `INFEASIBLE` | 硬约束互相冲突（**业务结果，不是崩溃**） | `./bin/sched diagnose <spec>` 读冲突候选与放宽建议 |
| DSH 报 `MISSING_CREDENTIAL` | headless 模板默认走 `deepseek-official`（要 `DEEPSEEK_API_KEY`） | 配 `DEEPSEEK_API_KEY`，或用安装脚本写入的 `deepseek-account` |

---

## 仓库包含什么

```
dsh-plugin/          DSH 插件（package.json + cordis.patch.yml + README）
.dsh/skills/         智能体技能（SKILL.md + references/）
src/scheduling_or/   内核（schema / model / solve / io / verify / cli / mcp_server）
config/              配置（base.yaml + 三套机构配置）
examples/            三套场景的合成样例规格 + 生成脚本
bin/                 sched（CLI）、sched-agent（智能体）、安装与自检脚本
tools/               安装脚本与插件自检的 Python 实现
```

**不包含**（本地开发材料，有意未发布）：单元与回归测试、调研与方案文档、原型脚本。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

> 注意：本项目的设计参考了若干开源项目，其中 `j3soon/nurse-scheduling` 与 FET 使用
> **AGPL-3.0**。本项目只借鉴其设计思路，**未复制任何代码**。
