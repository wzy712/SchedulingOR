---
name: scheduling-interview
description: 通过结构化访谈把用户口头的排班/排课需求固化成可校验的 YAML 规格，再用 sched CLI 或 MCP 工具完成校验、预检、求解、诊断与导出。用于排班、排课、值班表、值周表、轮班、监考安排、课后服务/晚自习辅导、roster、shift、schedule、constraints、fairness 等需求。触发后先访谈、再写规格、最后才求解 —— 绝不由模型生成求解代码。
whenToUse: 用户提出排班/排课/值班/监考/轮班需求，或要修改既有规格中的班次、需求、硬软约束、权重，或需要解释一份排不出来的班表
---

# 排班需求访谈与求解编排

## 0. 三条红线

1. **绝不写求解器代码**。你不写 Python、不改 `src/`。产物只有一份 YAML 规格（外加提问记录与报告解读）。
   表达不了的需求 → 记进 `unresolved` 问用户，**不要写代码绕过**。
2. **先 validate 再 solve**。`validate` 未通过（退出码 2）绝不允许 `solve`。
3. **硬约束不用大权重伪装**。违反硬约束 = 表发不出去 = INFEASIBLE；违反软约束 = 付代价、进报告。

## 1. 七个阶段

| 阶段 | 做什么 | 命令 |
|---|---|---|
| **P0 摸清数据** | 有哪些表、列怎么映射 | `import <xlsx> -i <场景>` → `lint <spec>` |
| **P1 访谈** | 逐轮提问变槽位（**每轮 ≤3 问，给选项**） | 产物 `specs/<name>.questions.md`（文件是真相源） |
| **P2 固化规格** | 写 `specs/<name>.yaml` 直到校验通过 | `validate <spec>` |
| **P3 干跑预检** | 纯算术自检，毫秒级，**必做** | `precheck <spec>` |
| **P4 正式求解** | 长任务放后台，主对话继续确认其它槽位 | `solve <spec> -i <场景> -o runs/` |
| **P5 解释** | 把结果与冲突翻译成人话 | `explain <run_dir>` / `diagnose <spec>` |
| **P6 导出 + 迭代** | 改文件不改代码 | `export <run_dir> -f xlsx` |

## 2. 访谈纪律（决定成败）

- **每轮 ≤3 问**。一轮问 12 个，用户只会答第一个。
- **给选项不给空白**。「连续上班上限怎么规定的」是坏问题；「**5 天** / **6 天** / **没规定**」是好问题。
- 读 `references/slots.md`（27 槽位 + 三场景映射）逐项确认。没有槽位表的 agent 只会问"你还有什么要求吗"。
- 能推断的就推断，但**必须写进 `spec.assumptions`**；问不出来的进 `spec.unresolved`。
  **用户沉默 ≠ 同意。**
- **收尾时产出「用户约束清单 ↔ 约束原语」双向映射让用户逐条确认。** 最危险的失败模式是
  模型自认为齐了、CP-SAT 报 OPTIMAL、实际漏了 2 条约束且**没有任何失败信号**。
- 字段拼错/日期越界 → 自动修 + 告知；需求人数/劳动规则/资质 → 必须问。

## 3. 槽位 → spec（完整表与三份范例见 `references/spec-schema.md`）

`horizon.start/.end` · `shifts[]` · `people[]`（`skills`/`attributes`/`history`/`max_shifts`）·
`groups[]` · `demand[]` · `unavailability[]` · `preferences[]`，加 `constraints.hard/soft`。

要点（踩了会静默出错）：

- **`demand` 每天每班都要有规则**。没声明的 `(date, shift)` = **不允许排班**（保护机制，不是限制）。
  表达"可排可不排"用 `min: 0, target: 0, max: N`。
- `min` = 软下限（重罚）；`target` = 理想值；**`max` = 硬上限**。`max` 是硬的，因为软上限会让
  「极差最小化」的公平目标诱导求解器往同一班次堆人。
- 同一班次不同日期不同人数 → 写多条带 `date` 的规则（显式日期优先于 `weekday`）。
- **跨月必须给 `history`**，否则月初的连续工作天数会从第 1 天重新计数。
- 约束只引用 **组 id**，不引用人员清单；组必须解析出**非空集合**（空组会直接报错）。
- `holidays` 默认**不**改变排班；要停排需显式设 `weekday_demand_applies_on_holidays: false`。
- `fairness.metric` 只能是 `total_shifts` / `weekend_shifts` / `holiday_shifts` / `shift:<id>`。
- `attributes` 的值必须是字符串；YAML 裸写 `yes`/`no` 会变布尔并报错，要加引号。

## 4. 调用顺序（实测语法）

**一律用 `<repo>/bin/sched`**（显式设 `PYTHONPATH`，不依赖 venv 安装状态）。
`-i` 只被 `solve` 与 `import` 接受；`diagnose` 收的是**规格**不是 run 目录。

```bash
bin/sched init <name>                        # 生成 specs/<name>.yaml + 机构配置骨架
bin/sched import data/staff.xlsx -i <场景> -o specs/ --json
bin/sched validate specs/a.yaml --json       # 不允许 solve，直到通过
bin/sched lint     specs/a.yaml --json       # 数据质量，有 error 会阻断
bin/sched precheck specs/a.yaml --json       # 毫秒级供需自检，必做
bin/sched solve    specs/a.yaml -i <场景> -o runs/ --json
bin/sched solve    specs/a.yaml -s solver.max_time_seconds=180 --json   # 超时/UNKNOWN 时
bin/sched explain  runs/<run_dir>/ --json    # 解释结果或冲突
bin/sched report   runs/<run_dir>/           # Markdown 报告（该命令没有 --json）
bin/sched diagnose specs/a.yaml --json       # 仅 INFEASIBLE 时；收规格
bin/sched export   runs/<run_dir>/ -f xlsx   # -f xlsx|csv|md
bin/sched doctor --json                      # 环境自检
```

结果走 stdout、进度走 stderr；能加 `--json` 就加，**解析 JSON 而不是抓文本**。

等价 MCP 工具（`mcp__schedulingor__<name>`）：`get_spec_schema` · `list_constraint_types` ·
`validate_spec` · `lint_instance` · `check_feasibility` · `solve` · `explain_infeasible` ·
`get_report` · `export_roster`。顺序：`get_spec_schema` → 写规格 → `validate_spec` →
`check_feasibility` → `solve(institution=...)` →（INFEASIBLE 则 `explain_infeasible`）→
`get_report` → `export_roster`。

`validate_spec` **返回结构化错误而不抛异常**（含 `loc`/`type`/`msg`）：读它、改规格、重试，
最多 3 轮；仍失败就把规格降级为 draft，把失败点写进 `unresolved` 请用户裁决。

## 5. 退出码 → 下一步

| 码 | 含义 | 你该做什么 |
|---|---|---|
| 0 | 成功 | 继续 |
| 1 | 内部错误 | **停止**，原文上报，不要重试、不要改规格掩盖 |
| 2 | 校验失败 / 用法错 | **修规格**后重试（≤3 轮） |
| 3 | **INFEASIBLE**（合法业务结论） | 去 `diagnose` / `explain_infeasible`，**不要重试 solve** |
| 4 | 超时未找到解（UNKNOWN） | 加时间盒或简化模型；**不要说"排不出来"** |

**INFEASIBLE ≠ UNKNOWN**：前者已证明无解，后者只是算不动。
诊断阶梯见 `references/troubleshooting.md`。**递进松弛每次都要用户确认并记进 `assumptions`**，
静默放宽 = 静默错误。

## 6. 怎么汇报（不允许只给一个目标值）

1. **状态**：`OPTIMAL` / `FEASIBLE`（未证明最优 → **必须报 gap**）/ `INFEASIBLE` / `UNKNOWN`。
2. **`verified`**：为 `false` 或 `verification_errors` 非空 → "模型对了但解错了"，
   **不要把结果交给用户**，原文上报。
3. **成本分解**：逐条软约束的 `violations`/`weight`/`cost`/占比。用户要看到**每项代价多少**，
   调权重才有依据。
4. **公平性**：`min`/`max`/`mean`/`spread`/`gini` + 逐人 `PersonStats`（总班次、`by_shift`、
   `weekend_shifts`、工时、偏好满足率）。
5. **覆盖对照**：哪些 `(date, shift)` 短编、哪些超编。
6. **导出路径**（宽表月历给人、长表给机器）。

`objective` 是**总加权代价**（等于成本表各行 `cost` 之和）；词典序分层求解下 `best_bound`
为 `null`，不要把它说成总代价的界。

## 7. 什么时候停下来问用户

- `validate` 连错 3 轮仍不通过。
- `INFEASIBLE` 且诊断给出 ≥2 条冲突候选 —— **让用户挑松哪一条**。
- 关键槽位缺失：跨月 `history` 为空、已批休假没给全、资质名单与用户口述不一致。
- `UNKNOWN` 且加过一轮时间盒仍无解。
- 规格里出现了你**推断**的内容而用户未确认。
- 输出格式、是否需要 PDF/公告版等用户没说的交付物。
