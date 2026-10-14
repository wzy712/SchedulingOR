# 领域槽位表：从业务问题到约束原语

> `scheduling-interview` 技能的按需参考。访谈**必须逐槽位确认**，不要问"你还有什么要求吗"。
> 表的用法：按「必问」列筛选 → 用「问法示例」发问（给选项，不给空白）→ 把答案落到「spec 字段」。
> **场景列只是同一原语在三个已播种场景里的落点，schema 本身与领域无关。**

## 0. 六个核心槽位（每单必问，且必须逐条落到约束）

| 槽位 | 落到 |
|---|---|
| 排班周期与班次定义 | `instance.horizon` / `instance.shifts[]` |
| 每日各班需求人数 | `instance.demand[]`（`date` 或 `weekday` 二选一） |
| 每人每天班次上限 | `hard[one_shift_per_day]` |
| 班次间最小休息 | `hard[rest_between_shifts]` |
| 连续工作上限 + 每周休息 | `hard[max_consecutive_work_days]` / `hard[weekly_rest_days]` |
| 人手不足时宁愿少排还是加班 | `soft[coverage_shortfall].weight` 与 `soft[coverage_excess].weight` 的**相对大小** |

## 1. 完整槽位表（27 槽）

「必问」：✅ = 每个项目都要问；⬜ = 有则问，无则跳过并写进 `unresolved`。

| # | 槽位 | 必问 | 业务问题（中文 / English） | spec 字段 | 约束原语 | 监考 | 值班 | 课后服务 |
|---|---|---|---|---|---|---|---|---|
| 1 | 排班周期 | ✅ | 「这次排的是**一个月**、**一周**，还是**某个考试周**？几号到几号？」/ "What date range should this cover — a month, a week, or an exam period?" | `instance.horizon.start` / `.end` | —（闭区间，含首尾） | 3 天考试周 | 3 月整月 | 4 周教学周 |
| 2 | 班次定义 | ✅ | 「一天分几个班次？**早/晚/夜**还是**按考试场次**？各几点到几点？」/ "How is a day divided — shifts or exam sessions — and what are the times?" | `instance.shifts[].id/.name/.start/.end/.hours` | —（`hours` 留空自动推算；跨零点用 `end < start`） | 一场考试=一个 shift | 早/晚/夜三班 | 学科×星期 |
| 3 | 每班需求人数 | ✅ | 「工作日早班要几个人？**周末一样吗**？有没有特殊日子？」/ "How many people per shift — same on weekends? any special dates?" | `instance.demand[].shift/.weekday/.date/.min/.target/.max` | `soft[coverage_shortfall]` + `soft[coverage_excess]` | 每场 2 人 | 早 2/晚 1/夜 1 | 每班 1 人 |
| 4 | 每人每天班次上限 | ✅ | 「允许一个人**一天上两个班**吗？」/ "Can one person work more than one shift in a day?" | — | `hard[one_shift_per_day].max_per_day`（默认 1） | 1 | 1 | 1 |
| 5 | 班次间最小休息 | ✅ | 「夜班结束后**至少要隔几小时**才能上下一班？」/ "Minimum rest hours between two shifts of the same person?" | `instance.shifts[].end` | `hard[rest_between_shifts].min_hours`（≤72） | 4h（考试期短） | 11h | 12h |
| 6 | 连续工作上限 | ✅ | 「最多**连续上几天**？第几天必须休？」/ "Longest allowed run of consecutive working days?" | `instance.people[].history[]`（跨月必填） | `hard[max_consecutive_work_days].days/.include_history` | 3（`include_history: false`） | 5（`true`） | 4（`false`） |
| 7 | 每周休息 | ✅ | 「每周**至少休几天**？」/ "Minimum rest days per week?" | — | `hard[weekly_rest_days].min`（1–7） | 1 | 1 | 1 |
| 8 | 非法接续 | ✅ | 「**夜班之后能直接上早班吗**？晚班接夜班呢？」/ "Is a night→morning turn illegal? evening→night?" | `instance.shifts[].id` | `hard[forbid_transition].from/.to`；软版本 `soft[penalized_transition]` | 数学连场回避 | 晚→夜禁止 | 通常无 |
| 9 | 工时上下限 | ⬜ | 「月度工时有没有**上下限**（如 140–184 小时）？」/ "Any monthly hours floor/ceiling?" | `instance.shifts[].hours` | `hard[hours_bounds].period/.min/.max/.group`（`week`/`horizon`） | 4–8h | 20–80h | 12–26h |
| 10 | 资质/岗位要求 | ⬜ | 「某个班次**必须有人持某某证/是某学科**吗？」/ "Does any shift require a specific qualification or subject?" | `instance.people[].skills` + `instance.groups[]` | `hard[requirement].shift/.group/.min`（**永远是硬约束**） | 学科教师监本科 | 夜班须党员+宿管 | 本学科辅导 |
| 11 | 新老搭配 / 回避 | ⬜ | 「新人**必须跟老员工一起**吗？有没有**两个人不能同班**？」/ "Must a junior be paired with a senior? Any two people who must not share a shift?" | `instance.groups[]` | `hard[pairing].junior_group/.senior_group`；`hard[no_pair].people` | 同一考场回避 | —— | 带教搭配 |
| 12 | 已批准休假 | ✅ | 「有没有**已经批了的假**、培训、出差？谁？哪几天？」/ "Any approved leave, training or off-site duty? who, which days?" | `instance.unavailability[].person/.dates/.date_range/.shifts`；简写 `instance.people[].unavailable` | 模型层强制（不可用即不可排） | 考试期间不可用 | 整段外出 | 病假一周 |
| 13 | 个人偏好 | ⬜ | 「大家有没有**想上/不想上**的班？有没有提交过偏好表？」/ "Any stated shift wishes or avoidances?" | `instance.preferences[].person/.shift/.kind/.date/.weekday/.weight` | `soft[preference].weight` | 想监本学科 | 不想上夜班 | 周五不答疑 |
| 14 | 公平性关注点 | ✅ | 「最在意哪一项公平：**总班次**、**夜班**、**周末**、还是**总工时**？」/ "Which fairness matters most — total shifts, nights, weekends, or hours?" | `instance.shifts[].id` | `soft[fairness].metric/.aggregation/.weight/.group` | 总班次 | 总班次 + `shift:night` | 分学科均衡 |
| 15 | 缺编 vs 超编 | ✅ | 「人手不够时，**宁愿少排人**（缺编）还是**让人加班/多排**（超编）？」/ "When short-staffed, prefer under-coverage or over-coverage?" | — | `soft[coverage_shortfall].weight` **>** `soft[coverage_excess].weight` 表示宁可超编 | 短编 1000 / 超编 20 | 同左 | 同左 |
| 16 | 输出格式 | ✅ | 「要**按人排的月历**（宽表），还是**按天的名单**（长表）？要 Excel 还是 CSV？」/ "Monthly grid per person, or a per-day list? Excel or CSV?" | —（`export` 参数） | — | 宽表+长表 | 宽表（张贴） | 长表（发给家长） |
| 17 | 跨月结转 | ⬜ | 「上个月最后几天谁上了班？**跨月的连续天数要不要连着算**？」/ "What did people work at the end of last month? Should streaks carry across the month boundary?" | `instance.people[].history[].date/.shift` | `hard[max_consecutive_work_days].include_history: true` | 少用 | **必问** | 少用 |
| 18 | 节假日与特殊日 | ⬜ | 「周期里有**节假日**吗？那天的需求跟平常一样吗？」/ "Any holidays in range? Does demand change on those days?" | `instance.holidays[]` + 一条 `instance.demand[].date` 覆盖规则 | `date` 规则**优先于** `weekday` 规则 | 考试日 | 妇女节加派 | 清明停排 |
| 19 | 固定安排 | ⬜ | 「有没有**已经定死**的人？比如某人某天必须值班/必须休息？」/ "Anything already fixed — a specific person on a specific day, or forced rest?" | — | `hard[fixed_assignment].person/.date/.shift`（`shift` 可填 `rest_code`） | 教务主任巡考 | 校长带班 | 教研组长坐班 |
| 20 | 个人班次上限 | ⬜ | 「有没有人**只能上很少的班**（如半职、孕期、临近退休）？」/ "Anyone with a personal cap on the number of shifts?" | `instance.people[].max_shifts` / `.min_shifts` / `.fte` | 模型层直接生效（horizon 级） | —— | 宿管 fte 0.5 | —— |
| 21 | 目标班次 | ⬜ | 「每人这个周期**理想上几个班**？要按人给不同目标吗？」/ "Ideal number of shifts per person? Different targets per person?" | `instance.people[].target_shifts` | `soft[target_shifts_deviation].default_target/.weight/.group` | 均衡 2 班 | 人均 14 班 | 人均 10 次 |
| 22 | 夜班强度 | ⬜ | 「夜班是不是**人人有份**？年轻人多上一点可以吗？」/ "Should nights be shared equally, or can junior staff take more?" | 建独立夜班组 `instance.groups[]` | `soft[fairness].metric: "shift:<夜班id>"` + `.group` | —— | `shift:night` 权重 60 | —— |
| 23 | 「谁不算人力」 | ✅ | 「实习生、新人、借调人员**算不算独立人力**？能单独派班吗？」/ "Do trainees / new hires / seconded staff count as independent capacity?" | `instance.people[].attributes`（如 `level: "junior"`） | `instance.groups[]` 具名集合（`diff`/`intersect`） | 新教师不算 | 宿管单列 | 实习教师不算 |
| 24 | 周期性上限 | ⬜ | 「每个人**每周最多几个班**？」/ "Any weekly cap on the number of shifts?" | — | `hard[max_shifts_per_period].period: week/.max/.group` | —— | 每周 ≤6 | 每周 ≤3 |
| 25 | 连续偏好（软） | ⬜ | 「是不是**不太希望**有人连上很多天，但也不是硬规定？」/ "Is a long streak discouraged but not forbidden?" | — | `soft[consecutive_work_preference].preferred_max/.weight` | 最长 2 天 | 最长 4 天 | 最长 3 天 |
| 26 | 跨机构/校区 | ⬜ | 「有没有**多个校区或考点**，同一个人不能同时出现在两处？」/ "Multiple sites or exam venues where one person cannot be in two places?" | 用不同 `instance.shifts[].id` 表达各处 | `hard[one_shift_per_day]` 天然覆盖同一天；必要时 `no_pair` | 多考点 | 多校区 | —— |
| 27 | 未决问题 | ✅ | 「以上有没有哪一条你**现在也不确定**、想之后再补？」/ "Anything you are unsure about and want to revisit later?" | `spec.assumptions[]` / `spec.unresolved[]` | —（**一等字段**，必须显式写出） | 是否学科回避 | 周末需求 | 假期是否停排 |

## 2. 三个场景的映射速查

同一份 schema，三套切分方式。**切分错了，后面所有约束都会别扭。**

| | 考试监考 | 教师值班 / 值周 | 课后服务 / 晚自习 |
|---|---|---|---|
| `horizon` | 考试周（3 天） | 整月（31 天） | 教学周（4 周） |
| `shifts[]` 粒度 | **一场考试**一个 id（`chinese1` / `math1` / `math2` / `english1`） | **早/晚/夜**三个 id，跨整月重复 | **学科 × 星期**一个 id（`math_mon` / `cn_tue` …） |
| `demand[]` 主键 | 几乎全部用 `date`（每场考试只在一天） | 几乎全部用 `weekday`（月度基线） | 全部用 `weekday`（每周重复） |
| `groups[]` 主要用途 | 按 `by_skill` 分学科（谁监哪科） | 按 `by_attribute` 分党员/班主任/宿管（谁带班） | 按 `by_skill` 分学科（谁能辅导） |
| 最关键的 3 条约束 | `requirement`(学科) / `one_shift_per_day` / `no_pair`(同考场回避) | `max_consecutive_work_days`+`history` / `rest_between_shifts` / `requirement`(党员带班) | `requirement`(本学科) / `max_shifts_per_period`(每周) / `hours_bounds` |
| 最容易漏的 | 一场考试一场地拆 id，别用"上午/下午" | **`history` 不填，月初连续天数就错** | 学科隔离只能靠 `requirement` **近似**（见 `spec-schema.md` §5 G2） |
| 软约束重点 | 监考次数公平（同一学科内） | 夜班公平 + 总次数公平 | 各学科内部均衡 + 目标次数 |
| 导出形态 | 按天的监考名单（长表） | 按人的月历（宽表，张贴用） | 按周的长表（通知家长） |

## 3. 提问纪律（写 SKILL.md 时被引用，这里是展开）

1. **每轮 ≤3 问**。一轮问 12 个问题，用户只会回答第一个。
2. **给选项，不给空白**。「连续上班上限你们是怎么规定的？」是坏问题；
   「连续上班上限是 **5 天**、**6 天**，还是**没规定**？」是好问题。
   表格第 6 槽的问法示例就写死了三个候选值。
3. **能推断的就推断，但必须写进 `assumptions`**。例如从班次时长推出"一天只能上一个班"，
   就写 `assumptions: [「每位教师每天只监一场；如允许上下午连排请告知」]`。
4. **问不出来的进 `unresolved`，绝不默默猜**。用户沉默 ≠ 同意。
5. **访谈状态落盘**：把待确认问题写进 `specs/<name>.questions.md`，
   **文件是真相源** —— 上下文被压缩、会话中断、换人接手都不丢进度。
   每条问题标 `necessary` / `exploratory`，否则访谈永不终止。
6. **收尾必须产出「用户约束清单 ↔ 约束原语实例」的双向映射，逐条请用户确认。**
   这是防"漏约束"唯一可靠的机制：最危险的失败模式是模型自己觉得齐了、CP-SAT 报
   OPTIMAL、但实际漏了 2 条约束且**没有任何失败信号**。用户点头的那份对照表就是验证器。
7. **明确区分「可自动修正」与「必须问用户」**：字段拼错、日期越界 → 自动改 + 告知；
   需求人数、劳动规则 → 必须问。
