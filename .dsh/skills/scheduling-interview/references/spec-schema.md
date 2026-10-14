# 规格字段字典与三份完整范例

> 本文件是 `scheduling-interview` 技能的按需参考，**不是**契约本身。
> 唯一的真相源是 `src/scheduling_or/schema/{spec,instance,constraints,solution}.py`。
> 下面的类型、必填、默认值列由脚本从 pydantic 模型自动读取生成；
> 三份范例均已通过 `scheduling_or.schema.Spec.loads()` 校验（见文末核对记录）。

## 0. 三条先记住的纪律

1. **`extra="forbid"`**：任何拼错的字段名会**直接报错**，不会被静默忽略。
   一个被丢掉的 `max_continous_days` 会变成"约束没生效"的静默错误。
   → 拿不准字段名就查本文件或 `mcp__schedulingor__get_spec_schema`，不要猜。
2. **约束只引用 `group`，不引用人员清单**。人员变动时只改组定义，约束不用动。
   `Group` 必须能解析出**非空集合**，否则报错（空组会让硬约束变成空操作）。
3. **硬约束与软约束是两种类型**，不是"同一个东西加上大权重"。
   违反硬约束 = 表发不出去 = 报 INFEASIBLE；违反软约束 = 付代价、进成本报告。

`schema_version` 当前只接受 `1`；其它值直接抛错并提示 `sched migrate`。

## 1. 顶层：`spec` 与 `solver`

**Spec — 顶层（`schema/spec.py`）**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `schema_version` | int | — | `1` | 契约版本；当前只接受 1，旧版本用 `sched migrate` 升级 |
| `name` | str | — | `` | 规格名，进入报告标题 |
| `instance` | Instance | ✅ | — | 事实层（人、班次、日期、需求） |
| `constraints` | ConstraintSet | — | 对象（各字段均有默认值） | 策略层，`hard` / `soft` 两个列表 |
| `solver` | SolverParams | — | 对象（各字段均有默认值） | 求解器旋钮，与约束分离便于调参 |
| `assumptions` | list[str] | — | 空 | **一等字段**：agent 的每一条推断都必须写进来待用户复核 |
| `unresolved` | list[str] | — | 空 | **一等字段**：问不出来、留待用户补充的点，绝不默默猜 |

**`instance` — Instance（`schema/instance.py`）**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `horizon` | Horizon | ✅ | — | 排班覆盖的闭区间日期范围 |
| `shifts` | list[Shift] | ✅ | — | 日内的具名班次；跨场景统一叫 shift |
| `people` | list[Person] | ✅ | — | 可分配资源（教师/护士/值班员） |
| `groups` | list[Group] | — | 空 | 具名组，**约束只引用组 id，不引用人员清单** |
| `demand` | list[DemandRule] | — | 空 | 覆盖需求：按 `date` 或 `weekday` 给基线 |
| `unavailability` | list[UnavailabilityRule] | — | 空 | 已批准的假：请假、培训、出差、回避 |
| `preferences` | list[Preference] | — | 空 | 个人想上/不想上的班 |
| `holidays` | list[date] | — | 空 | 节假日日期；`weekday` 类需求规则仍按星期几匹配，不会被自动跳过 |
| `rest_code` | str | — | `__off__` | 休息占位码；不占人力，也不出现在输出里 |

**`instance.horizon` — Horizon**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `start` | date | ✅ | — | 含首日 |
| `end` | date | ✅ | — | 含末日；早于 start 直接报错 |

**`instance.shifts[]` — Shift**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | str | ✅ | — | `min_length=1`；约束、需求、偏好里引用的班次键 |
| `name` | str | — | `` | 中文显示名（早班/夜班/语文监考） |
| `start` | str | ✅ | — | `HH:MM` |
| `end` | str | ✅ | — | `HH:MM`；可早于 `start` 表示跨零点 |
| `hours` | float \\| NoneType | — | `null` | `gt=0`；`le=24`；留空则按 start/end 推算；跨零点自动 +24h |
| `attendance` | bool | — | `true` | `false` 表示 OFF 之类的伪班次，不消耗人力 |

**`instance.people[]` — Person**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | str | ✅ | — | `min_length=1`；稳定工号；约束与解全部按 id 关联 |
| `name` | str | — | `` | 姓名 |
| `role` | str | — | `` | 岗位；`by_role` 组选择器用 |
| `skills` | list[str] | — | 空 | 资质标签；`by_skill` 组选择器用 |
| `attributes` | dict[str, str] | — | 空 | 自定义分类（`title`/`party`/`subject`…）；`by_attribute` 用。**值必须是字符串** |
| `max_shifts` | int \\| NoneType | — | `null` | `ge=0`；整个 horizon 的个人硬上限 |
| `min_shifts` | int \\| NoneType | — | `null` | `ge=0`；整个 horizon 的个人硬下限 |
| `target_shifts` | int \\| NoneType | — | `null` | `ge=0`；软目标值，由 `target_shifts_deviation` 消费 |
| `fte` | float | — | `1.0` | `gt=0`；`le=1.0`；工时系数，取值 (0, 1] |
| `history` | list[HistoryEntry] | — | 空 | **上月结转班次**；跨月连续工作天数规则依赖它 |
| `unavailable` | list[str] | — | 空 | 简写形式的不可用：`"YYYY-MM-DD"`（整天）或 `"YYYY-MM-DD:<shift>"` |

**`instance.people[].history[]` — HistoryEntry**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `date` | date | ✅ | — | 历史班次日期，应早于 `horizon.start` |
| `shift` | str | ✅ | — | 当时上的班次 id |

**`instance.groups[]` — Group**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | str | ✅ | — | `min_length=1`；约束引用的组名 |
| `name` | str | — | `` | 中文显示名 |
| `all` | bool | — | `false` | 全体人员 |
| `by_role` | str \\| NoneType | — | `null` | 按 `role` 精确匹配 |
| `by_skill` | str \\| NoneType | — | `null` | 成员 `skills` 含该标签 |
| `by_attribute` | dict[str, str] | — | 空 | 按 `attributes` 的键值对精确匹配 |
| `explicit` | list[str] | — | 空 | 显式人员 id 列表 |
| `union` | list[str] | — | 空 | 并入其它组 id |
| `intersect` | list[str] | — | 空 | 与其它组 id 求交 |
| `diff` | list[str] | — | 空 | 减去其它组 id；**必须有基础集，不能只有 diff** |

**`instance.demand[]` — DemandRule**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `shift` | str | ✅ | — | 目标班次 id |
| `date` | date \\| NoneType | — | `null` | 具体日期规则；与 `weekday` **二选一必填** |
| `weekday` | mon / tue / wed / thu / fri / sat / sun \\| NoneType | — | `null` | 星期几基线规则；与 `date` 二选一必填 |
| `min` | int | — | `0` | `ge=0`；硬下限（通常由 `coverage_shortfall` 兜底为软目标） |
| `target` | int \\| NoneType | — | `null` | `ge=0`；目标人数；`target < min` 直接报错 |
| `max` | int \\| NoneType | — | `null` | `ge=0`；上限；`max < min` 直接报错 |
| `note` | str | — | `` | 给人看的备注 |

**`instance.unavailability[]` — UnavailabilityRule**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `person` | str | ✅ | — | 人员 id |
| `dates` | list[date] | — | 空 | 具体日期列表 |
| `date_range` | tuple[date, date] \\| NoneType | — | `null` | `[起, 止]` 闭区间二元组 |
| `shifts` | list[str] | — | 空 | 留空 = 整天不可用；否则只针对列出的班次 |
| `reason` | str | — | `` | 原因，会进报告 |

**`instance.preferences[]` — Preference**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `person` | str | ✅ | — | 人员 id |
| `shift` | str | ✅ | — | 班次 id |
| `kind` | want / avoid | — | `want` | `want` 奖励满足 / `avoid` 惩罚命中 |
| `date` | date \\| NoneType | — | `null` | 与 `weekday` 最多出现一个；都不给 = 每天都适用 |
| `weekday` | mon / tue / wed / thu / fri / sat / sun \\| NoneType | — | `null` | 星期几重复出现 |
| `weight` | int | — | `1` | `ge=1`；本人该条偏好的相对强度（≥1），与 `preference.weight` 相乘 |
| `note` | str | — | `` | 备注 |

**`solver` — SolverParams**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `max_time_seconds` | float | — | `60.0` | `gt=0`；`le=3600`；时间盒；长求解走独立子进程，超时被 kill |
| `num_workers` | int | — | `8` | `ge=1`；`le=64`；CP-SAT 并行 worker；≥8 才触发并行搜索 |
| `random_seed` | int | — | `0` | `ge=0`；复现凭据之一 |
| `strategy` | lexicographic / weighted_sum | — | `lexicographic` | `lexicographic` 先证可行再优化 / `weighted_sum` 单目标 |
| `relative_gap_limit` | float | — | `0.0` | `ge=0`；`le=1`；提前停止的相对 gap |
| `log_search_progress` | bool | — | `false` | CP-SAT 原生日志（写 stderr） |
| `freeze_feasible` | bool | — | `true` | 词典序下把阶段①解固定为常量再优化软目标 |
## 2. 权重刻度约定

| 语义 | 量级 | 说明 |
|---|---|---|
| 业务上"绝不该违反"的软项 | 1e3 以上，最高 1e12 | `coverage_shortfall` 默认 1000；再往上要显式说明理由 |
| 公平性 | 1e3 | `fairness` 默认 50，多指标时注意总量 |
| 偏好 / 模式 | 1e0 ~ 1e2 | `preference` 30、`consecutive_work_preference` 10 |
| 停用 | 0 | `weight: 0` 是**停用**，不是删除，便于 A/B 对比 |

- **权重一律整数**。CP-SAT 的约束里禁止浮点系数，全整数是唯一稳妥选择。
- **硬约束绝不进权重表**。`weight` 只能出现在软约束上。
- 报告必须给出**逐项加权贡献**，否则权重无法被理性调整。

## 3. 硬约束 / 软约束字段

所有硬/软约束共享三个字段：`id`（string，留空自动派生为 `type#n`）、`enabled`（bool，默认 true）、`note`（string，默认 ""）。判别键是 `type`。以下只列各自特有字段。

**`one_shift_per_day`** — OneShiftPerDay（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `max_per_day` | int | — | `1` | `ge=1`；每人每天最多几个真实班次 |

**`rest_between_shifts`** — RestBetweenShifts（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `min_hours` | float | — | `0.0` | `ge=0`；`le=72`；上一班结束到下一班开始的最小间隔（小时） |

**`max_consecutive_work_days`** — MaxConsecutiveWorkDays（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `days` | int | ✅ | — | `ge=1`；允许的最长连续上班天数 |
| `include_history` | bool | — | `true` | 是否把 `people[].history` 计入；跨月必开 |

**`forbid_transition`** — ForbidTransition（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `from` | str | ✅ | — | YAML 键是 `from`（Python 属性名 `from_shift`） |
| `to` | str | ✅ | — | YAML 键是 `to`（Python 属性名 `to_shift`） |

**`weekly_rest_days`** — WeeklyRestDays（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `min` | int | ✅ | — | `ge=1`；`le=7`；每 7 天内至少休息几天 |

**`max_shifts_per_period`** — MaxShiftsPerPeriod（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `period` | week / horizon | — | `horizon` | `week` = 自然周 / `horizon` = 整个周期 |
| `max` | int | ✅ | — | `ge=1`；周期内班次上限 |
| `group` | str \\| NoneType | — | `null` | 只约束该组；留空 = 全体 |

**`hours_bounds`** — HoursBounds（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `period` | week / horizon | — | `horizon` | `week` / `horizon` |
| `min` | float \\| NoneType | — | `null` | `ge=0`；周期工时下限；与 max 至少给一个 |
| `max` | float \\| NoneType | — | `null` | `gt=0`；周期工时上限 |
| `group` | str \\| NoneType | — | `null` | 只约束该组 |

**`requirement`** — Requirement（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `shift` | str | ✅ | — | 被约束的班次 id |
| `group` | str | ✅ | — | 该班次每天至少要有该组几人；**资质/岗位硬要求就用这条** |
| `min` | int | — | `1` | `ge=1`；≥1 |
| `max` | int \\| NoneType | — | `null` | `ge=1`；可选的同组人数上限 |

**`fixed_assignment`** — FixedAssignment（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `person` | str | ✅ | — | 被钉住的人 |
| `date` | str | ✅ | — | `"YYYY-MM-DD"` |
| `shift` | str | ✅ | — | 钉到该班次；也可填 `rest_code` 表示强制休息 |

**`no_pair`** — NoPair（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `people` | list[str] | ✅ | — | `min_length=2`；至少 2 人；这些人不得同一天同一班 |

**`pairing`** — Pairing（硬约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `junior_group` | str | ✅ | — | 新手组 id |
| `senior_group` | str | ✅ | — | 带教/老手组 id；新手**只有在**同班有老手时才能被排 |

**`coverage_shortfall`** — CoverageShortfall（软约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `weight` | int | — | `1000` | `ge=0`；缺编惩罚，应**压过所有其它软项** |

**`coverage_excess`** — CoverageExcess（软约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `weight` | int | — | `20` | `ge=0`；超编惩罚，通常远小于缺编 |

**`fairness`** — Fairness（软约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `metric` | str | — | `total_shifts` | `total_shifts` 或 `shift:<班次id>` |
| `aggregation` | spread / deviation | — | `spread` | `spread` 极差 max-min / `deviation` 与均值的绝对偏差 |
| `weight` | int | — | `50` | `ge=0`；公平性权重，量级 1e3 以内 |
| `group` | str \\| NoneType | — | `null` | 只对组内成员平衡；留空 = 全体 |

**`preference`** — Preference（软约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `weight` | int | — | `30` | `ge=0`；偏好未满足的单价（需配合 `instance.preferences`） |

**`consecutive_work_preference`** — ConsecutiveWorkPreference（软约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `preferred_max` | int | ✅ | — | `ge=1`；软性偏好的最长连续上班天数 |
| `weight` | int | — | `10` | `ge=0`；超出的单价 |

**`penalized_transition`** — PenalizedTransition（软约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `from` | str | ✅ | — | YAML 键是 `from`（Python 属性名 `from_shift`） |
| `to` | str | ✅ | — | YAML 键是 `to`（Python 属性名 `to_shift`） |
| `weight` | int | — | `10` | `ge=0`；单次计罚 |

**`target_shifts_deviation`** — TargetShiftsDeviation（软约束）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `default_target` | int \\| NoneType | — | `null` | `ge=0`；未设 `person.target_shifts` 时的默认目标 |
| `weight` | int | — | `30` | `ge=0`；偏离 1 个班次的单价 |
| `group` | str \\| NoneType | — | `null` | 只对组内成员生效 |
## 4. 三份完整范例

三份规格覆盖三个已播种的学校教务场景，**共用同一套原语**。
它们都通过了 `Spec.loads()` + `validate_ids_referenced()` 校验，可以直接作为起点复制修改
（把 `name` / `instance` 换成真实数据即可，`constraints` 的形状通常不用改）。

场景与建模要点：

| 场景 | 主时隙如何切分 | 关键原语 | 容易踩的坑 |
|---|---|---|---|
| 考试监考 | **一场考试 = 一个 shift id**（`chinese1`/`math1`/`math2`/`english1`），而不是"上午/下午" | `requirement` 把学科教师绑到本学科考场；`fixed_assignment`；`no_pair` | 同一班次在不同日期需要不同人数时，`DemandRule` 表达不了 —— 必须拆成不同 shift id |
| 教师值班 | 早/晚/夜三班 **跨整月重复**，靠 `weekday` 需求规则给基线、`date` 规则给例外 | `max_consecutive_work_days` + `history`；`rest_between_shifts`；`weekly_rest_days` | 跨月必须填 `people[].history`，否则月初连续天数算错 |
| 课后服务 | **学科 × 星期 = 一个 shift id**（`math_mon`/`cn_tue`…） | `requirement` 按学科分组建约束；`hours_bounds` 控总量 | 教师只能上本学科班，schema 没有"资质匹配"原语，只能用 `requirement` 近似（见 §5） |

### 4.1 范例一：考试监考（2025-06 高二期末考试）

```yaml
schema_version: 1
name: 2025-06 高二年级期末考试监考
instance:
  horizon: { start: 2025-06-07, end: 2025-06-09 }
  shifts:
    - { id: chinese1, name: 语文监考, start: "09:00", end: "11:30", hours: 2.5 }
    - { id: math1,    name: 数学监考, start: "15:00", end: "17:00", hours: 2.0 }
    - { id: math2,    name: 数学监考, start: "09:00", end: "11:00", hours: 2.0 }
    - { id: english1, name: 英语监考, start: "15:00", end: "17:00", hours: 2.0 }
  people:
    - { id: T001, name: 张伟, role: teacher, skills: [chinese], attributes: { subject: chinese, title: 高级 } }
    - { id: T002, name: 李娜, role: teacher, skills: [chinese], attributes: { subject: chinese } }
    - { id: T003, name: 王强, role: teacher, skills: [math],    attributes: { subject: math } }
    - { id: T004, name: 赵敏, role: teacher, skills: [math],    attributes: { subject: math } }
    - { id: T005, name: 陈静, role: teacher, skills: [english], attributes: { subject: english, title: 高级, party: "yes" } }
    - { id: T006, name: 刘洋, role: teacher, skills: [english], attributes: { subject: english } }
    - { id: T007, name: 孙丽, role: teacher, skills: [chinese], attributes: { subject: chinese, party: "yes" } }
    - { id: T008, name: 周涛, role: staff,  skills: [admin],   attributes: { subject: "none" } }
  groups:
    - { id: chinese_teachers, name: 语文教师, by_skill: chinese }
    - { id: math_teachers,    name: 数学教师, by_skill: math }
    - { id: english_teachers, name: 英语教师, by_skill: english }
    - { id: exam_office,      name: 教务考务组, explicit: [T007, T008] }
    - { id: leadership,       name: 领导班子, by_attribute: { title: 高级 } }
    - { id: party_members,    name: 党员教师, by_attribute: { party: "yes" } }
  demand:
    - { date: 2025-06-07, shift: chinese1, min: 2, target: 2, max: 2, note: 语文考场 2 个 }
    - { date: 2025-06-07, shift: math1,    min: 2, target: 2, max: 2 }
    - { date: 2025-06-08, shift: math2,    min: 2, target: 2, max: 2 }
    - { date: 2025-06-08, shift: english1, min: 2, target: 2, max: 2 }
    - { weekday: mon, shift: chinese1, min: 2, target: 2, max: 2, note: 常规周考基线 }
  unavailability:
    - { person: T003, dates: [2025-06-08], reason: 外出培训 }
    - { person: T001, date_range: [2025-06-08, 2025-06-08], shifts: [english1], reason: 回避本班英语考试 }
  preferences:
    - { person: T002, shift: chinese1, kind: want, weight: 2, note: 希望监本学科 }
    - { person: T004, shift: math1, kind: avoid, weight: 1, note: 已连排两天 }
  holidays: []
  rest_code: "__off__"
constraints:
  hard:
    - { id: h_one_shift, type: one_shift_per_day, max_per_day: 1 }
    - { id: h_rest, type: rest_between_shifts, min_hours: 4 }
    - { id: h_max_total, type: max_shifts_per_period, period: horizon, max: 3 }
    - { id: h_no_consec_math, type: forbid_transition, from: math1, to: math2 }
    - { id: h_req_chinese, type: requirement, shift: chinese1, group: chinese_teachers, min: 1 }
    - { id: h_req_math1, type: requirement, shift: math1, group: math_teachers, min: 1 }
    - { id: h_req_math2, type: requirement, shift: math2, group: math_teachers, min: 1 }
    - { id: h_req_english, type: requirement, shift: english1, group: english_teachers, min: 1 }
    - { id: h_leader_present, type: requirement, shift: chinese1, group: leadership, min: 1 }
    - { id: h_fixed_t007, type: fixed_assignment, person: T007, date: "2025-06-07", shift: chinese1 }
    - { id: h_no_pair, type: no_pair, people: [T003, T004], note: 同一考场需回避 }
    - { id: h_hours, type: hours_bounds, period: horizon, min: 4, max: 8 }
  soft:
    - { id: s_shortfall, type: coverage_shortfall, weight: 1000 }
    - { id: s_excess, type: coverage_excess, weight: 20 }
    - { id: s_fair_total, type: fairness, metric: total_shifts, aggregation: spread, weight: 50 }
    - { id: s_fair_pref, type: preference, weight: 30 }
    - { id: s_streak, type: consecutive_work_preference, preferred_max: 2, weight: 10 }
solver: { max_time_seconds: 30, num_workers: 8, random_seed: 0, strategy: lexicographic }
assumptions:
  - 每位教师每天只监一场；如允许上下午连排请告知
  - 6 月 9 日无考试，horizon 末尾保留是为了跨日连续工作天数计算
unresolved:
  - 是否需要按学科回避（语文教师不监语文）？当前未加此硬约束
```

### 4.2 范例二：教师值班表（2025-03，跨月 + history）

```yaml
schema_version: 1
name: 2025-03 教师值班表
instance:
  horizon: { start: 2025-03-01, end: 2025-03-31 }
  shifts:
    - { id: early, name: 早班, start: "07:00", end: "13:00", hours: 6 }
    - { id: late,  name: 晚班, start: "13:00", end: "19:00", hours: 6 }
    - { id: night, name: 夜班, start: "19:00", end: "23:00", hours: 4 }
  people:
    - { id: T001, name: 张伟, role: teacher,     skills: [admin],   fte: 1.0, max_shifts: 18, min_shifts: 12, target_shifts: 16, attributes: { party: "yes", dorm: "yes" }, history: [{ date: 2025-02-27, shift: early }, { date: 2025-02-28, shift: early }] }
    - { id: T002, name: 李娜, role: teacher,     skills: [admin],   fte: 1.0, max_shifts: 16, target_shifts: 14, attributes: { party: "yes" }, unavailable: ["2025-03-08", "2025-03-09:night"] }
    - { id: T003, name: 王强, role: teacher,     skills: [admin],   fte: 0.8, max_shifts: 16, target_shifts: 14, attributes: { head_teacher: "yes" }, history: [{ date: 2025-02-28, shift: night }] }
    - { id: T004, name: 赵敏, role: teacher,     skills: [admin],   fte: 1.0, max_shifts: 16, target_shifts: 14, attributes: { head_teacher: "yes" } }
    - { id: T005, name: 陈静, role: teacher,     skills: [admin],   fte: 1.0, max_shifts: 16, target_shifts: 14, attributes: {} }
    - { id: T008, name: 周涛, role: dorm_staff,  skills: [admin],   fte: 0.5, max_shifts: 12, target_shifts: 8,  attributes: { dorm: "yes" }, unavailable: ["2025-03-15", "2025-03-16", "2025-03-17"] }
  groups:
    - { id: all_duty,     name: 全部值班人员, all: true }
    - { id: teachers,     name: 教师岗,       by_role: teacher }
    - { id: party,        name: 党员,         by_attribute: { party: "yes" } }
    - { id: head_teachers, name: 班主任,      by_attribute: { head_teacher: "yes" } }
    - { id: dorm,         name: 宿舍管理,     by_attribute: { dorm: "yes" } }
  demand:
    - { weekday: mon, shift: early, min: 2, target: 2, max: 3, note: 工作日早班 2 人 }
    - { weekday: mon, shift: late,  min: 1, target: 1, max: 2 }
    - { weekday: mon, shift: night, min: 1, target: 1, max: 2 }
    - { weekday: tue, shift: early, min: 2, target: 2, max: 3 }
    - { weekday: tue, shift: late,  min: 1, target: 1, max: 2 }
    - { weekday: tue, shift: night, min: 1, target: 1, max: 2 }
    - { weekday: wed, shift: early, min: 2, target: 2, max: 3 }
    - { weekday: wed, shift: late,  min: 1, target: 1, max: 2 }
    - { weekday: wed, shift: night, min: 1, target: 1, max: 2 }
    - { weekday: thu, shift: early, min: 2, target: 2, max: 3 }
    - { weekday: thu, shift: late,  min: 1, target: 1, max: 2 }
    - { weekday: thu, shift: night, min: 1, target: 1, max: 2 }
    - { weekday: fri, shift: early, min: 2, target: 2, max: 3 }
    - { weekday: fri, shift: late,  min: 1, target: 1, max: 2 }
    - { weekday: fri, shift: night, min: 2, target: 2, max: 3, note: 周五夜班加强 }
    - { weekday: sat, shift: early, min: 1, target: 1, max: 2 }
    - { weekday: sat, shift: late,  min: 1, target: 1, max: 2 }
    - { weekday: sat, shift: night, min: 1, target: 1, max: 2 }
    - { weekday: sun, shift: early, min: 1, target: 1, max: 2 }
    - { weekday: sun, shift: late,  min: 1, target: 1, max: 2 }
    - { weekday: sun, shift: night, min: 1, target: 1, max: 2 }
    - { date: 2025-03-08, shift: night, min: 2, target: 3, max: 3, note: 妇女节加派 }
  unavailability:
    - { person: T002, dates: [2025-03-08, 2025-03-09], shifts: [night], reason: 已批准调休 }
    - { person: T008, date_range: [2025-03-15, 2025-03-17], reason: 宿舍整修外出 }
  preferences:
    - { person: T001, shift: early, kind: want, weight: 2, note: 希望多排早班 }
    - { person: T005, shift: night, kind: avoid, weight: 3, note: 家中有事 }
    - { person: T008, shift: night, weekday: fri, kind: avoid, weight: 2 }
  holidays: []
  rest_code: "__off__"
constraints:
  hard:
    - { id: h_one_shift, type: one_shift_per_day, max_per_day: 1 }
    - { id: h_rest, type: rest_between_shifts, min_hours: 11 }
    - { id: h_streak, type: max_consecutive_work_days, days: 5, include_history: true }
    - { id: h_weekly_rest, type: weekly_rest_days, min: 1 }
    - { id: h_transition, type: forbid_transition, from: late, to: night, note: 晚班后不得接夜班 }
    - { id: h_max_period, type: max_shifts_per_period, period: week, max: 6, group: all_duty }
    - { id: h_hours, type: hours_bounds, period: horizon, min: 20, max: 80 }
    - { id: h_req_party_night, type: requirement, shift: night, group: party, min: 1, note: 夜班须有党员带班 }
    - { id: h_req_head_early, type: requirement, shift: early, group: head_teachers, min: 1 }
    - { id: h_req_dorm, type: requirement, shift: night, group: dorm, min: 1, note: 夜班须有宿舍管理在岗 }
  soft:
    - { id: s_shortfall, type: coverage_shortfall, weight: 1000 }
    - { id: s_excess, type: coverage_excess, weight: 20 }
    - { id: s_fair_total, type: fairness, metric: total_shifts, aggregation: spread, weight: 50 }
    - { id: s_fair_night, type: fairness, metric: "shift:night", aggregation: spread, weight: 60 }
    - { id: s_pref, type: preference, weight: 30 }
    - { id: s_target, type: target_shifts_deviation, default_target: 14, weight: 30 }
    - { id: s_streak, type: consecutive_work_preference, preferred_max: 4, weight: 10 }
solver: { max_time_seconds: 60, num_workers: 8, random_seed: 0, strategy: lexicographic }
assumptions:
  - 夜班 19:00-23:00 算 4 小时；若需宿舍留宿值守请改为 8 小时并告知
  - 每人每天只值一个班次
unresolved:
  - 周末需求是否与工作日一致？当前已按较低配置
```

### 4.3 范例三：课后服务 / 晚自习辅导（2025-03，按学科分班）

```yaml
schema_version: 1
name: 2025-03 课后服务与晚自习辅导排班
instance:
  horizon: { start: 2025-03-03, end: 2025-03-30 }
  shifts:
    - { id: math_mon, name: 周一数学辅导, start: "16:30", end: "18:30", hours: 2 }
    - { id: math_wed, name: 周三数学辅导, start: "16:30", end: "18:30", hours: 2 }
    - { id: cn_tue,   name: 周二语文辅导, start: "16:30", end: "18:30", hours: 2 }
    - { id: cn_thu,   name: 周四语文辅导, start: "16:30", end: "18:30", hours: 2 }
    - { id: qa_fri,   name: 周五答疑,     start: "16:30", end: "17:30", hours: 1 }
  people:
    - { id: T101, name: 王强, role: tutor, skills: [math],    max_shifts: 13, min_shifts: 6, target_shifts: 10, attributes: { subject: math } }
    - { id: T102, name: 赵敏, role: tutor, skills: [math],    max_shifts: 13, min_shifts: 6, target_shifts: 10, attributes: { subject: math } }
    - { id: T201, name: 张伟, role: tutor, skills: [chinese], max_shifts: 13, min_shifts: 6, target_shifts: 10, attributes: { subject: chinese } }
    - { id: T202, name: 李娜, role: tutor, skills: [chinese], max_shifts: 13, min_shifts: 6, target_shifts: 10, attributes: { subject: chinese } }
  groups:
    - { id: all_tutors,  name: 全部辅导教师, all: true }
    - { id: math_tutors,    name: 数学教师, by_skill: math }
    - { id: chinese_tutors, name: 语文教师, by_skill: chinese }
    - { id: qa_capable,     name: 可答疑教师, union: [math_tutors, chinese_tutors] }
  demand:
    - { weekday: mon, shift: math_mon, min: 1, target: 1, max: 1 }
    - { weekday: wed, shift: math_wed, min: 1, target: 1, max: 1 }
    - { weekday: tue, shift: cn_tue,   min: 1, target: 1, max: 1 }
    - { weekday: thu, shift: cn_thu,   min: 1, target: 1, max: 1 }
    - { weekday: fri, shift: qa_fri,   min: 1, target: 2, max: 2, note: 周五答疑 1-2 人 }
  unavailability:
    - { person: T102, dates: [2025-03-10], reason: 家访 }
    - { person: T202, date_range: [2025-03-24, 2025-03-28], reason: 病假 }
  preferences:
    - { person: T101, shift: math_mon, kind: want, weight: 2 }
    - { person: T201, shift: qa_fri, kind: avoid, weight: 2, note: 周五需接送孩子 }
  holidays: []
  rest_code: "__off__"
constraints:
  hard:
    - { id: h_one_shift, type: one_shift_per_day, max_per_day: 1 }
    - { id: h_rest, type: rest_between_shifts, min_hours: 12 }
    - { id: h_streak, type: max_consecutive_work_days, days: 4, include_history: false }
    - { id: h_weekly_rest, type: weekly_rest_days, min: 1 }
    - { id: h_max_week, type: max_shifts_per_period, period: week, max: 3, group: all_tutors }
    - { id: h_hours, type: hours_bounds, period: horizon, min: 12, max: 26 }
    - { id: h_req_math_mon, type: requirement, shift: math_mon, group: math_tutors, min: 1, note: 学科教师只能上本学科 }
    - { id: h_req_math_wed, type: requirement, shift: math_wed, group: math_tutors, min: 1 }
    - { id: h_req_cn_tue, type: requirement, shift: cn_tue, group: chinese_tutors, min: 1, note: 同上，仅本学科教师 }
    - { id: h_req_cn_thu, type: requirement, shift: cn_thu, group: chinese_tutors, min: 1 }
    - { id: h_req_qa, type: requirement, shift: qa_fri, group: qa_capable, min: 1 }
  soft:
    - { id: s_shortfall, type: coverage_shortfall, weight: 1000 }
    - { id: s_excess, type: coverage_excess, weight: 20 }
    - { id: s_fair_total, type: fairness, metric: total_shifts, aggregation: spread, weight: 50 }
    - { id: s_fair_math, type: fairness, metric: "shift:math_mon", aggregation: spread, weight: 40, group: math_tutors }
    - { id: s_fair_cn, type: fairness, metric: "shift:cn_tue", aggregation: deviation, weight: 40, group: chinese_tutors }
    - { id: s_target, type: target_shifts_deviation, default_target: 10, weight: 30 }
    - { id: s_pref, type: preference, weight: 30 }
solver: { max_time_seconds: 30, num_workers: 8, random_seed: 0, strategy: lexicographic }
assumptions:
  - 每位教师每周最多 3 次辅导（周一/周三数学 + 周五答疑）
  - 学科教师只承担本学科辅导，答疑不限学科
unresolved:
  - 4 月起的清明假期是否需要停排？horizon 只到 3 月 30 日
```

## 5. schema 表达不了的东西（重要，遇到要主动告诉用户）

以下是本次建模三个场景时**实际撞到**的边界。不是 bug，但必须知道，否则会写出"看起来对、
其实没约束住"的规格。**每一条都对应一句该对用户说的话。**

| # | 限制 | 后果 | 工作区/替代做法 | 该对用户说的话 |
|---|---|---|---|---|
| G1 | `DemandRule` 的匹配键是 `(shift, date\|weekday)`，且"具体日期胜过星期几" | **同一个 shift 在不同日期无法给不同人数** | 拆成不同 shift id（范例一用 `math1`/`math2` 区分两场数学考试） | 「同一门课的分班考试我会建成两个班次，这样人数才能分开放」 |
| G2 | 没有"资质匹配"原语：任何人**原则上**都能被排到任何班次 | "只让数学老师辅导数学"无法逐人强制 | `requirement(shift, group, min=N)` + `demand.min=N` 做到"至少 N 名该学科教师"；靠组与需求人数相等来逼近 | 「我保证每班至少 1 名本学科教师，但无法从数学上禁止别的科目老师被临时排进来；如需绝对禁止请单独说明」 |
| G3 | `requirement` **永远是硬约束**，没有权重，也不吃 `coverage_shortfall` 的软目标 | 资质人数不足 → 直接 INFEASIBLE，而不是"先缺着、报代价" | 想软处理就不要写 `requirement`，改成"提高该组 `fairness`/`target_shifts_deviation` 权重 + 接受短编" | 「如果某学科教师当天真的不够，我会报「排不出来」而不是偷偷少排，这样可以吗？」 |
| G4 | `fairness.metric` 只支持 `total_shifts` 与 `shift:<班次id>` | **无法直接表达"周末班次数"或"节假日班次数"** | 把周末/节假日的班建成独立 shift id（如 `weekend_night`），再用 `shift:weekend_night` | 「周末班要单独建一类班次才能做均衡，我会这样建」 |
| G5 | 只有 `max_consecutive_work_days`，**没有 `min_consecutive_work_days`**（方案 §5.4 列了但 schema 未提供） | 无法强制"拒绝碎片班"（如必须连上≥2 天） | 只能靠 `consecutive_work_preference(preferred_max)` 反向表达，或人工复核 | 「我无法强制『必须连上两天以上』，只能给你看连续天数的分布」 |
| G6 | `instance.holidays` **只是数据，不改变约束语义** | `weekday` 类需求规则在节假日照常生效；元旦走 `mon` 的配置 | 节假日必须显式再写一条 `date` 规则覆盖它（`demand_for()` 里 `date` 优先） | 「节假日我会单独写一条需求覆盖，否则它会沿用星期几的配置」 |
| G7 | `holidays` / `unavailability` / 需求规则之间的冲突**不做自动检查** | 可能排出一张"节假日没人值班"或"请假的人被排班"的规格 | 靠 `sched precheck` 与 `lint` 的供需算术自检兜底 | ——（对用户不必说，但 agent 必须依赖 precheck） |
| G8 | `Person.unavailable`（简写）与 `instance.unavailability[]`（规则）**两套机制并存** | 语义重复，容易只写一边 | 统一用 `instance.unavailability[]`；简写只用于快速草稿 | —— |
| G9 | `Group.diff` 必须有基础集，且组**不能解析为空** | 想建"排除某人"的组必须先有母组；一个成员都不匹配的组会直接报错 | 先建 `all: true` 母组，再 `diff: [某组]` | —— |
| G10 | `attributes` / `by_attribute` 的值类型是 `dict[str, str]` | YAML 里裸写 `party: yes` 会被解析成布尔 `True` 从而**报错** | 值一律加引号：`party: "yes"`；同理 `dorm: "yes"`、`subject: "none"` | —— |
| G11 | `max_consecutive_work_days` 的 `days: N` 在"每人每天最多一班"下同时隐含"每 N+1 天必须休一天" | 高需求场景下会与 `demand.min` 打架造成 INFEASIBLE | 先用 `precheck` 比 `demand` 总量与可用人力上限；必要时调大 N | 「连续上班上限 3 天意味着 4 天里必须休 1 天，需求可能因此排不满」 |
| G12 | 没有"半天/时段内最长连续工作时长"之类的概念 | 无法表达"一次值班不超过 4 小时"（除班次本身切分） | 用切分班次表达 | —— |

## 6. 核对记录

- 三份范例均以 `Spec.loads(text, "yaml")` 通过 pydantic 校验，且
  `validate_text()` 返回 `ok: true`（`stage: done`），`validate_ids_referenced()` 无问题。
- 另做了 `Spec.loads(spec.dumps("yaml"))` 的**往返一致性**检查，`model_dump()` 完全相等
  —— 保证 `forbid_transition.from` / `to` 这类带别名的字段在导出后仍能读回。
- 生成与校验脚本不在仓库内（写在 `.gen/`，验证后已删除）；字段表中的类型/默认值由
  `pydantic.BaseModel.model_fields` 直接读取，人工只补中文说明。
