# (This module deliberately emits Chinese text: full-width punctuation here is
#  user-facing output, never a confusable-character typo.)
"""CSV mechanics **and** the canonical tabular views rendered by both exporters.

Two layers live here on purpose:

* **CSV mechanics** -- :func:`read_csv_table`, :func:`read_csv_rows`,
  :func:`write_csv_rows`, :func:`write_csv_tables`.  Writing is always UTF-8
  *with BOM* (``utf-8-sig``): Excel on Windows silently mis-decodes BOM-less
  UTF-8 as the local ANSI code page.  Reading accepts a BOM whether or not it is
  present, so files round-trip either way.
* **Tabular views** -- :func:`readme_table`, :func:`roster_view`,
  :func:`assignments_table`, :func:`coverage_table`, :func:`fairness_table`,
  :func:`violations_table`.  ``excel_export`` turns these into worksheets and
  :func:`write_csv_tables` turns the very same objects into CSV files, so the
  XLSX and CSV deliverables can never drift apart.

A "table" is ``list[list[Any]]`` with the header as row 0 -- exactly what both
``csv.writer`` and XlsxWriter consume.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from ..schema.instance import Instance, Shift
from ..schema.solution import CoverageRow, FairnessMetric, PersonStats, Solution
from ..schema.spec import Spec

__all__ = [
    "CSV_ENCODING",
    "RosterView",
    "assignment_counts",
    "assignments_table",
    "coverage_table",
    "distribution_stats",
    "fairness_table",
    "read_csv_rows",
    "read_csv_table",
    "readme_table",
    "roster_table",
    "roster_view",
    "shift_hours",
    "shift_label",
    "violations_table",
    "weekday_cn",
    "write_csv_rows",
    "write_csv_tables",
]

CSV_ENCODING = "utf-8-sig"
"""Files are written UTF-8-with-BOM so double-clicking them in Excel works."""

BOM = "\ufeff"

Table: TypeAlias = list[list[Any]]

WEEKDAY_CN: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# --------------------------------------------------------------------------
# CSV mechanics
# --------------------------------------------------------------------------
def read_csv_table(path: str | Path) -> Table:
    """Read a CSV file into raw rows (``str`` cells); BOM-aware.

    Trailing all-empty rows are dropped; inner blank rows are kept so that
    spreadsheet row numbers stay meaningful to the user.
    """
    with Path(path).open("r", encoding=CSV_ENCODING, newline="") as handle:
        rows: Table = [list(row) for row in csv.reader(handle)]
    while rows and all(cell.strip() == "" for cell in rows[-1]):
        rows.pop()
    return rows


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    """Read a CSV file into dicts keyed by the header row.

    Used by the importers, so a CSV and an XLSX go through one reader.  Rows
    that are entirely blank are skipped.
    """
    table = read_csv_table(path)
    if not table:
        return []
    header = [str(cell) for cell in table[0]]
    records: list[dict[str, str]] = []
    for row in table[1:]:
        record = {
            name: (row[index] if index < len(row) else "") for index, name in enumerate(header)
        }
        if any(value.strip() for value in record.values()):
            records.append(record)
    return records


def write_csv_rows(path: str | Path, rows: Iterable[Sequence[Any]]) -> Path:
    """Write ``rows`` (header first) as UTF-8-with-BOM CSV.  Returns the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(["" if cell is None else cell for cell in row])
    return target


def write_csv_tables(out_dir: str | Path, tables: Mapping[str, Table]) -> list[Path]:
    """Write one ``<name>.csv`` per table, in mapping order.  Returns the paths."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return [write_csv_rows(directory / f"{name}.csv", rows) for name, rows in tables.items()]


# --------------------------------------------------------------------------
# Small lookups shared by every view
# --------------------------------------------------------------------------
def weekday_cn(date: dt.date) -> str:
    """``周一`` .. ``周日``."""
    return WEEKDAY_CN[date.weekday()]


def _shift(instance: Instance, shift_id: str) -> Shift | None:
    for shift in instance.shifts:
        if shift.id == shift_id:
            return shift
    return None


def shift_label(instance: Instance, shift_id: str) -> str:
    """Human label for a shift id; the rest code renders as blank."""
    if shift_id == instance.rest_code:
        return ""
    shift = _shift(instance, shift_id)
    if shift is None:
        return shift_id
    return shift.name or shift.id


def shift_hours(instance: Instance, shift_id: str) -> float:
    """Hours of one shift (0.0 for unknown ids or the rest code)."""
    shift = _shift(instance, shift_id)
    return 0.0 if shift is None else shift.resolved_hours()


def _person_name(instance: Instance, person_id: str) -> str:
    for person in instance.people:
        if person.id == person_id:
            return person.name or person_id
    return person_id


def _person_role(instance: Instance, person_id: str) -> str:
    for person in instance.people:
        if person.id == person_id:
            return person.role
    return ""


def assignment_counts(solution: Solution) -> dict[tuple[dt.date, str], int]:
    """``(date, shift) -> assigned people``, ignoring rest-code assignments."""
    counts: dict[tuple[dt.date, str], int] = {}
    for assignment in solution.assignments:
        key = (assignment.date, assignment.shift)
        counts[key] = counts.get(key, 0) + 1
    return counts


def distribution_stats(values: Sequence[float | int]) -> dict[str, float]:
    """min/max/mean/spread/stddev/gini for the per-person counts of one metric."""
    data = [float(value) for value in values]
    if not data:
        return {
            "minimum": 0.0,
            "maximum": 0.0,
            "mean": 0.0,
            "spread": 0.0,
            "stddev": 0.0,
            "gini": 0.0,
        }
    count = len(data)
    total = sum(data)
    mean = total / count
    variance = sum((value - mean) ** 2 for value in data) / count
    if total > 0:
        weighted = sum((index + 1) * value for index, value in enumerate(sorted(data)))
        gini = (2 * weighted) / (count * total) - (count + 1) / count
    else:
        gini = 0.0
    return {
        "minimum": min(data),
        "maximum": max(data),
        "mean": round(mean, 4),
        "spread": max(data) - min(data),
        "stddev": round(variance**0.5, 4),
        "gini": round(max(0.0, gini), 4),
    }


# --------------------------------------------------------------------------
# README
# --------------------------------------------------------------------------
def readme_table(
    solution: Solution,
    instance: Instance,
    spec: Spec | None = None,
    *,
    legend: Mapping[str, str] | None = None,
    layout: str = "",
    generated_at: dt.datetime | None = None,
) -> Table:
    """Key/value header sheet: parameters, solver outcome, and the agent's caveats."""
    stamp = generated_at or dt.datetime.now()
    if solution.verified:
        verification = "通过"
    elif solution.verification_errors:
        verification = f"未通过（{len(solution.verification_errors)} 条错误）"
    else:
        verification = "未执行（结果不可信，涉事班次已标红）"

    rows: Table = [
        ["项目", "值"],
        ["规格名称", (spec.name if spec is not None else "") or "（未命名）"],
        ["求解状态", solution.status],
        ["是否可行", "是" if solution.feasible else "否"],
        ["目标值", solution.objective],
        ["最优界", solution.best_bound],
        ["求解时长(秒)", round(solution.wall_time_s, 3)],
        ["布尔变量数", solution.num_booleans],
        ["冲突数", solution.num_conflicts],
        ["搜索分支数", solution.branches],
        ["独立校验", verification],
        ["排班区间", f"{instance.horizon.start} ~ {instance.horizon.end}"],
        ["天数", len(instance.dates())],
        ["人数", len(instance.people)],
        ["班次数", len(instance.shift_ids)],
        ["休息代码", instance.rest_code],
        ["导出视图", layout or "monthly"],
        ["生成时间", stamp.strftime("%Y-%m-%d %H:%M:%S")],
    ]

    def section(title: str, items: Iterable[str]) -> None:
        values = [str(item) for item in items if str(item)]
        if not values:
            return
        rows.append([])
        rows.append([title, ""])
        rows.extend([[str(index), value] for index, value in enumerate(values, start=1)])

    section("【假设 assumptions】", spec.assumptions if spec is not None else [])
    section("【待确认 unresolved】", spec.unresolved if spec is not None else [])
    section("【求解备注 notes】", solution.notes)
    section("【不可行诊断 diagnosis】", [solution.diagnosis] if solution.diagnosis else [])
    section("【冲突 conflicts】", solution.conflicts)
    section("【建议 suggestions】", solution.suggestions)
    section("【校验错误 verification_errors】", solution.verification_errors)

    if legend:
        rows.append([])
        rows.append(["【班次配色】", "同一班次在本工作簿所有 sheet 中颜色一致"])
        rows.extend(
            [[shift_label(instance, shift_id), colour] for shift_id, colour in legend.items()]
        )
    return rows


# --------------------------------------------------------------------------
# Wide / monthly roster
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RosterView:
    """The wide grid's data, independent of any renderer.

    ``rows`` carries ``len(leading_headers) + len(dates) + len(trailing_headers)``
    cells each: leading person columns, one cell per date holding the shift **id**
    (or blank), then the totals columns.  Renderers turn ids into labels with
    :func:`shift_label`, so the XLSX grid can colour by id while the CSV prints
    the human name.
    """

    leading_headers: list[str]
    trailing_headers: list[str]
    dates: list[dt.date]
    rows: list[list[Any]]


def roster_view(solution: Solution, instance: Instance) -> RosterView:
    """Person x date grid; every person in the instance gets a row, in instance order."""
    by_person: dict[str, dict[dt.date, str]] = {}
    for assignment in solution.assignments:
        if assignment.shift == instance.rest_code:
            continue
        by_person.setdefault(assignment.person, {})[assignment.date] = assignment.shift

    dates = instance.dates()
    rows: list[list[Any]] = []
    for person in instance.people:
        assigned = by_person.get(person.id, {})
        cells = [assigned.get(date, "") for date in dates]
        hours = sum(shift_hours(instance, shift) for shift in assigned.values())
        rows.append(
            [
                person.name or person.id,
                person.id,
                person.role,
                *cells,
                len(assigned),
                round(hours, 3),
            ]
        )
    return RosterView(
        leading_headers=["姓名", "工号", "岗位"],
        trailing_headers=["合计班次", "合计工时"],
        dates=dates,
        rows=rows,
    )


def roster_table(solution: Solution, instance: Instance) -> Table:
    """CSV form of the wide grid: one header row, one row per person."""
    view = roster_view(solution, instance)
    header = [
        *view.leading_headers,
        *[f"{date.month}/{date.day} {weekday_cn(date)}" for date in view.dates],
        *view.trailing_headers,
    ]
    split = len(view.leading_headers)
    dates_end = split + len(view.dates)
    rows = [
        [
            *row[:split],
            *[shift_label(instance, str(cell)) if cell else "" for cell in row[split:dates_end]],
            *row[dates_end:],
        ]
        for row in view.rows
    ]
    return [header, *rows]


# --------------------------------------------------------------------------
# Long / tidy view
# --------------------------------------------------------------------------
def assignments_table(solution: Solution, instance: Instance) -> Table:
    """One row per (person, date, shift)."""
    holidays = set(instance.holidays)
    rows: Table = [
        [
            "人员ID",
            "姓名",
            "岗位",
            "日期",
            "星期",
            "班次ID",
            "班次名称",
            "开始",
            "结束",
            "工时",
            "周末",
            "节假日",
        ]
    ]
    for assignment in sorted(solution.assignments, key=lambda a: (a.date, a.person, a.shift)):
        shift = _shift(instance, assignment.shift)
        rows.append(
            [
                assignment.person,
                _person_name(instance, assignment.person),
                _person_role(instance, assignment.person),
                assignment.date.isoformat(),
                weekday_cn(assignment.date),
                assignment.shift,
                shift_label(instance, assignment.shift),
                shift.start if shift is not None else "",
                shift.end if shift is not None else "",
                shift_hours(instance, assignment.shift),
                "是" if assignment.date.weekday() >= 5 else "",
                "是" if assignment.date in holidays else "",
            ]
        )
    return rows


# --------------------------------------------------------------------------
# Coverage / fairness / violations
# --------------------------------------------------------------------------
def coverage_rows(solution: Solution, instance: Instance) -> list[CoverageRow]:
    """The full date x shift grid, using the solver's numbers wherever it reported them.

    A partial ``Solution.coverage`` must not shrink the sheet: the whole point of
    the Coverage view is to show every slot, so solver rows are overlaid onto the
    grid derived from ``Instance.demand`` and the assignments.
    """
    reported = {(row.date, row.shift): row for row in solution.coverage}
    counts = assignment_counts(solution)
    dates = instance.dates()
    rows: list[CoverageRow] = []
    for date in dates:
        for shift_id in instance.shift_ids:
            if (given := reported.get((date, shift_id))) is not None:
                rows.append(given)
                continue
            rule = instance.demand_for(date, shift_id)
            rows.append(
                CoverageRow(
                    date=date,
                    shift=shift_id,
                    minimum=rule.min if rule is not None else 0,
                    target=rule.target if rule is not None else None,
                    maximum=rule.max if rule is not None else None,
                    assigned=counts.get((date, shift_id), 0),
                )
            )
    grid = {(date, shift_id) for date in dates for shift_id in instance.shift_ids}
    rows.extend(row for key, row in reported.items() if key not in grid)
    return sorted(rows, key=lambda row: (row.date, row.shift))


def coverage_table(solution: Solution, instance: Instance) -> Table:
    """Date x shift demand vs. assigned, with the shortfall/excess the schema derives."""
    rows: Table = [
        [
            "日期",
            "星期",
            "班次ID",
            "班次名称",
            "最小",
            "目标",
            "最大",
            "已排",
            "缺口",
            "超出",
            "备注",
        ]
    ]
    for row in coverage_rows(solution, instance):
        rule = instance.demand_for(row.date, row.shift)
        rows.append(
            [
                row.date.isoformat(),
                weekday_cn(row.date),
                row.shift,
                shift_label(instance, row.shift),
                row.minimum,
                row.target,
                row.maximum,
                row.assigned,
                row.shortfall,
                row.excess,
                rule.note if rule is not None else "",
            ]
        )
    return rows


def _derived_person_stats(solution: Solution, instance: Instance) -> dict[str, PersonStats]:
    """Per-person counts computed from the assignments, used when the solver is silent."""
    holidays = set(instance.holidays)
    stats: dict[str, PersonStats] = {}
    for assignment in solution.assignments:
        entry = stats.setdefault(assignment.person, PersonStats())
        entry.shifts += 1
        entry.hours = round(entry.hours + shift_hours(instance, assignment.shift), 4)
        entry.by_shift[assignment.shift] = entry.by_shift.get(assignment.shift, 0) + 1
        if assignment.date.weekday() >= 5 or assignment.date in holidays:
            entry.weekend_shifts += 1
    return stats


def _fairness_metrics(solution: Solution, counts: Mapping[str, int]) -> list[FairnessMetric]:
    """Solver-provided metrics, or one derived ``shifts`` metric as a fallback."""
    if solution.fairness or not counts:
        return list(solution.fairness)
    stats = distribution_stats(list(counts.values()))
    return [
        FairnessMetric(
            metric="shifts",
            per_person=dict(counts),
            minimum=int(stats["minimum"]),
            maximum=int(stats["maximum"]),
            mean=stats["mean"],
            spread=int(stats["spread"]),
            stddev=stats["stddev"],
            gini=stats["gini"],
        )
    ]


def fairness_table(solution: Solution, instance: Instance) -> Table:
    """Two sections: per-person statistics, then each fairness metric's summary."""
    stats = dict(solution.per_person) or _derived_person_stats(solution, instance)
    metrics = _fairness_metrics(solution, {person: entry.shifts for person, entry in stats.items()})

    rows: Table = [["一、按人员统计"]]
    rows.append(
        [
            "人员ID",
            "姓名",
            "岗位",
            "班次",
            "工时",
            "周末班次",
            "偏好申请",
            "偏好满足",
            "满足率",
            *[f"公平:{metric.metric}" for metric in metrics],
        ]
    )
    order = [person.id for person in instance.people if person.id in stats]
    order += sorted(person for person in stats if person not in set(order))
    for person_id in order:
        entry = stats[person_id]
        rate = entry.preference_rate
        rows.append(
            [
                person_id,
                _person_name(instance, person_id),
                _person_role(instance, person_id),
                entry.shifts,
                entry.hours,
                entry.weekend_shifts,
                entry.preferences_requested,
                entry.preferences_met,
                "" if rate is None else rate,
                *[metric.per_person.get(person_id, "") for metric in metrics],
            ]
        )

    rows.append([])
    rows.append(["二、公平性指标"])
    rows.append(["指标", "最小", "最大", "均值", "极差", "标准差", "基尼", "说明"])
    for metric in metrics:
        rows.append(
            [
                metric.metric,
                metric.minimum,
                metric.maximum,
                metric.mean,
                metric.spread,
                metric.stddev,
                metric.gini,
                "由导出层推算（求解器未提供）" if not solution.fairness else "",
            ]
        )
    return rows


def violations_table(solution: Solution, instance: Instance | None = None) -> Table:
    """Soft-goal cost breakdown, then the independent verifier's findings."""
    total = solution.total_cost
    rows: Table = [["一、软约束代价"]]
    rows.append(["约束ID", "族", "标签", "违反次数", "权重", "代价", "占比"])
    for term in solution.cost_breakdown:
        rows.append(
            [
                term.constraint_id,
                term.family,
                term.label,
                term.violations,
                term.weight,
                term.cost,
                round(term.cost / total, 4) if total else 0.0,
            ]
        )
    rows.append(["合计", "", "", "", "", total, 1.0 if total else 0.0])

    rows.append([])
    rows.append(["二、独立校验", "通过" if solution.verified else "未通过或未执行"])
    rows.append(["来源", "内容"])
    if not solution.verification_errors:
        rows.append(["verification", "无错误记录" if solution.verified else "未执行独立校验"])
    for message in solution.verification_errors:
        rows.append(["verification", message])
    for label, items in (
        ("conflict", solution.conflicts),
        ("suggestion", solution.suggestions),
        ("note", solution.notes),
        ("diagnosis", [solution.diagnosis] if solution.diagnosis else []),
    ):
        rows.extend([label, item] for item in items)
    return rows
