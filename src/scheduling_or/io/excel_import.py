# (This module deliberately emits Chinese text: full-width punctuation here is
#  user-facing output, never a confusable-character typo.)
"""User workbook -> schema objects, driven by an explicit config-declared mapping.

User spreadsheets are messy and never match our schema, so import is deliberately
two steps: the *mapping* (which user column feeds which schema field) is declared
in config, and this module applies it.  Nothing here guesses.

Failure policy -- the reader is a 教务员, not an engineer:

* a coercion failure **never** raises; it produces a problem string that names the
  sheet, the spreadsheet row, the column header, the schema field and the person,
  e.g. ``工作表「人员名单」 第 5 行，列「工时系数」（字段 fte），人员=T03：无法把 'abc' 识别为小数``
* a row whose *key* (工号/班次号) is missing or broken is skipped; a row with a
  broken non-key cell is kept with the default value and reported, so one typo
  cannot silently drop a person
* the loaders still return ``list[...]`` as documented, but the concrete type is
  :class:`Loaded`, a ``list`` subclass carrying ``.problems`` -- the diagnostics
  travel with the data instead of being written to a log nobody reads

The horizon is a *fact* that often lives in the workbook, so
:func:`import_instance` reads it from an optional 校历 sheet, and otherwise infers
it from the explicit dates found in the other sheets.  Callers should record that
inference in ``Spec.assumptions``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from openpyxl import load_workbook
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..schema.instance import (
    DemandRule,
    HistoryEntry,
    Horizon,
    Person,
    Preference,
    Shift,
    UnavailabilityRule,
    Weekday,
)
from .csv_io import read_csv_table

__all__ = [
    "CoercionError",
    "ColumnBinding",
    "ColumnMap",
    "ImportResult",
    "InstanceParts",
    "Loaded",
    "PreferenceKind",
    "Row",
    "SheetMap",
    "SheetTable",
    "WorkbookMap",
    "clean_text",
    "format_time",
    "import_instance",
    "load_dates",
    "load_demand",
    "load_holidays",
    "load_horizon",
    "load_preferences",
    "load_shifts",
    "load_staff",
    "load_unavailability",
    "parse_bool",
    "parse_date",
    "parse_float",
    "parse_int",
    "parse_list",
    "parse_time",
    "parse_weekday",
    "read_sheets",
]

PreferenceKind = Literal["want", "avoid"]
T = TypeVar("T")
TModel = TypeVar("TModel", bound=BaseModel)


class CoercionError(ValueError):
    """A cell could not be converted; ``str(exc)`` is the user-facing reason."""


class Loaded(list[T]):
    """A ``list`` of imported objects that also carries row-located problems.

    Subclassing keeps the documented ``-> list[Person]`` contract (it *is* a
    list) while making rows that were skipped or defaulted visible.

    A ``list`` passed as ``problems`` is kept **by reference**: the loaders keep
    appending to their local sink while parsing, and the caller must see those
    later entries.  Copying here would silently drop every problem found after
    the object was created.
    """

    def __init__(self, items: Iterable[T] = (), problems: list[str] | None = None) -> None:
        super().__init__(items)
        self.problems: list[str] = problems if problems is not None else []


# ==========================================================================
# Cell value coercion  (public: lint.py reuses these)
# ==========================================================================
_FULL_WIDTH_RANGE = ("\uff01", "\uff5e")
_SPACE_LIKE = ("\u3000", "\xa0", "\u2007", "\u202f", "\u200b")


def _half_width(text: str) -> str:
    """Map full-width ASCII (２０２５，．) onto its ASCII twin."""
    low, high = _FULL_WIDTH_RANGE
    return "".join(chr(ord(char) - 0xFEE0) if low <= char <= high else char for char in text)


def clean_text(value: Any) -> str:
    """Cell -> trimmed text; full-width/NBSP spaces become ordinary spaces.

    Integral floats lose the ``.0`` Excel appends to numeric cells, so the 工号
    ``1001`` does not become ``1001.0``.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    text = _half_width(str(value))
    for char in _SPACE_LIKE:
        text = text.replace(char, " ")
    return text.strip()


def _is_blank(value: Any) -> bool:
    return clean_text(value) == ""


_EXCEL_EPOCH = dt.date(1899, 12, 30)  # 1900 system, including the fake 1900-02-29
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y%m%d")
_ANNOTATION = re.compile(r"[（(][^)）]*[)）]")
_WEEKDAY_SUFFIX = re.compile(r"(周|星期|礼拜)[一二三四五六日天1-7]")


def parse_date(value: Any) -> dt.date:
    """``datetime`` / Excel serial / ``2025-03-01`` / ``2025/3/1`` / ``2025年3月1日``."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, bool):
        raise CoercionError(f"{value!r} 不是日期")
    if isinstance(value, (int, float)):
        serial = float(value)
        if 1 <= serial <= 2958465:
            return _EXCEL_EPOCH + dt.timedelta(days=int(serial))
        raise CoercionError(f"数字 {value} 不像是 Excel 日期序列号")
    text = _WEEKDAY_SUFFIX.sub("", _ANNOTATION.sub("", clean_text(value))).strip()
    if not text:
        raise CoercionError("日期为空")
    token = text.split()[0]
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    raise CoercionError(f"无法把 {text!r} 识别为日期（请写成 2025-03-01 或 2025/3/1）")


def parse_time(value: Any) -> dt.time:
    """``time`` / Excel day fraction / ``8:00`` / ``8点30分`` / ``0830``."""
    if isinstance(value, dt.time):
        return value
    if isinstance(value, dt.datetime):
        return value.time()
    if isinstance(value, bool):
        raise CoercionError(f"{value!r} 不是时间")
    if isinstance(value, (int, float)):
        fraction = float(value)
        if 0 <= fraction < 1:
            minutes = round(fraction * 24 * 60)
            return dt.time(minutes // 60 % 24, minutes % 60)
        raise CoercionError(f"数字 {value} 不像是 Excel 时间（应为 0~1 的小数）")
    text = (
        clean_text(value).replace("：", ":").replace("点", ":").replace("时", ":").replace("分", "")
    )
    if not text:
        raise CoercionError("时间为空")
    parts = text.split(":")
    if len(parts) == 1:
        digits = parts[0]
        if not digits.isdigit():
            raise CoercionError(f"无法把 {text!r} 识别为时间（请写成 08:00）")
        width = len(digits)
        if width <= 2:
            hour, minute, second = int(digits), 0, 0
        elif width == 3:
            hour, minute, second = int(digits[0]), int(digits[1:]), 0
        elif width == 4:
            hour, minute, second = int(digits[:2]), int(digits[2:]), 0
        else:
            raise CoercionError(f"无法把 {text!r} 识别为时间（请写成 08:00）")
    else:
        try:
            hour = int(parts[0] or 0)
            minute = int(parts[1] or 0) if len(parts) > 1 else 0
            second = int(parts[2] or 0) if len(parts) > 2 else 0
        except ValueError:
            raise CoercionError(f"无法把 {text!r} 识别为时间（请写成 08:00）") from None
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise CoercionError(f"时间 {text!r} 超出 00:00~23:59 范围")
    return dt.time(hour, minute, second)


def format_time(value: dt.time) -> str:
    """``dt.time`` -> the ``'HH:MM'`` string the schema stores."""
    return f"{value.hour:02d}:{value.minute:02d}"


_TRUE_TOKENS = frozenset(
    {"是", "y", "yes", "t", "true", "1", "1.0", "√", "✓", "对", "有", "需要", "参加"}
)
_FALSE_TOKENS = frozenset(
    {"否", "n", "no", "f", "false", "0", "0.0", "×", "x", "✗", "无", "不需要", "不参加"}
)


def parse_bool(value: Any) -> bool:
    """``是/否/Y/N/TRUE/FALSE/1/0/√/×`` (and full-width twins)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    text = clean_text(value).casefold()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    raise CoercionError(f"无法把 {value!r} 识别为是/否（可用 是、否、Y、N、TRUE、FALSE、1、0）")


def parse_int(value: Any) -> int:
    """Integers only: ``3``, ``3.0`` and ``'3'`` pass; ``2.5`` is an error, not a truncation."""
    if isinstance(value, bool):
        raise CoercionError(f"{value!r} 不是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise CoercionError(f"{value} 不是整数")
    text = clean_text(value).replace(",", "")
    if not text:
        raise CoercionError("数值为空")
    try:
        number = float(text)
    except ValueError:
        raise CoercionError(f"无法把 {text!r} 识别为整数") from None
    if not number.is_integer():
        raise CoercionError(f"无法把 {text!r} 识别为整数")
    return int(number)


def parse_float(value: Any) -> float:
    """``0.8``; a trailing ``%`` is honoured so ``80%`` means ``0.8``."""
    if isinstance(value, bool):
        raise CoercionError(f"{value!r} 不是小数")
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value).replace(",", "")
    if not text:
        raise CoercionError("数值为空")
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        number = float(text)
    except ValueError:
        raise CoercionError(f"无法把 {text!r} 识别为小数") from None
    return round(number / 100, 6) if percent else number


_LIST_SEPARATOR = re.compile(r"[、,;；|\r\n]+")


def parse_list(value: Any) -> list[str]:
    """``'A、B'`` / ``'A,B'`` / ``'A；B'`` -> ``['A', 'B']`` (order preserved, deduped)."""
    if value is None:
        return []
    items: list[str] = []
    for chunk in _LIST_SEPARATOR.split(clean_text(value)):
        item = chunk.strip()
        if item and item not in items:
            items.append(item)
    return items


_WEEKDAY_BY_CHAR: dict[str, Weekday] = {
    "一": "mon",
    "二": "tue",
    "三": "wed",
    "四": "thu",
    "五": "fri",
    "六": "sat",
    "日": "sun",
    "天": "sun",
}
_WEEKDAY_BY_INDEX: dict[str, Weekday] = {
    "1": "mon",
    "2": "tue",
    "3": "wed",
    "4": "thu",
    "5": "fri",
    "6": "sat",
    "7": "sun",
}
_WEEKDAY_EN: dict[str, Weekday] = {
    "mon": "mon",
    "monday": "mon",
    "tue": "tue",
    "tues": "tue",
    "tuesday": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "thu": "thu",
    "thur": "thu",
    "thursday": "thu",
    "fri": "fri",
    "friday": "fri",
    "sat": "sat",
    "saturday": "sat",
    "sun": "sun",
    "sunday": "sun",
}


def parse_weekday(value: Any) -> Weekday:
    """``周一``/``星期一``/``周1``/``mon`` -> the schema's ``Weekday`` literal."""
    text = clean_text(value).casefold()
    if not text:
        raise CoercionError("星期为空")
    if text in _WEEKDAY_EN:
        return _WEEKDAY_EN[text]
    stem = re.sub(r"^(周|星期|礼拜)", "", text).strip()
    if stem in _WEEKDAY_BY_CHAR:
        return _WEEKDAY_BY_CHAR[stem]
    if stem in _WEEKDAY_BY_INDEX:
        return _WEEKDAY_BY_INDEX[stem]
    raise CoercionError(f"无法把 {value!r} 识别为星期（请写成 周一 ~ 周日）")


# ==========================================================================
# Mapping models (the config contract from docs/plans §4.5)
# ==========================================================================
class ColumnMap(BaseModel):
    """One logical field <- one source column (header name, or 0-based index)."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    column: str | int

    @model_validator(mode="after")
    def _not_bool(self) -> ColumnMap:
        if isinstance(self.column, bool):  # pydantic folds bool into int otherwise
            raise ValueError("column must be a header name or a 0-based index, not a boolean")
        if isinstance(self.column, int) and self.column < 0:
            raise ValueError(f"column index {self.column} is negative; indexes are 0-based")
        return self


class SheetMap(BaseModel):
    """One sheet: where its header is, and which column feeds which field."""

    model_config = ConfigDict(extra="forbid")

    sheet: str = Field(min_length=1)
    header_row: int = Field(default=1, ge=1, description="1-based, as the user sees it")
    columns: list[ColumnMap]
    skip_rows: int = Field(
        default=1, ge=0, description="rows to ignore directly under the header (e.g. a units row)"
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_plan_shorthand(cls, data: Any) -> Any:
        """Accept the ``columns: {id: 工号, name: 姓名}`` form used in docs/plans §4.5."""
        if isinstance(data, dict):
            columns = data.get("columns")
            if isinstance(columns, Mapping):
                return {
                    **data,
                    "columns": [{"field": key, "column": value} for key, value in columns.items()],
                }
        return data


class WorkbookMap(BaseModel):
    """Which sheet feeds which part of an :class:`~scheduling_or.schema.instance.Instance`."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _accept_plan_synonyms(cls, data: Any) -> Any:
        """``requests`` is what docs/plans §4.5 calls the preferences sheet; accept both."""
        if isinstance(data, dict):
            synonyms = {"requests": "preferences", "leaves": "unavailability", "catalog": "shifts"}
            return {synonyms.get(key, key): value for key, value in data.items()}
        return data

    staff: SheetMap | None = None
    shifts: SheetMap | None = None
    demand: SheetMap | None = None
    unavailability: SheetMap | None = None
    preferences: SheetMap | None = None
    holidays: SheetMap | None = None
    horizon: SheetMap | None = Field(
        default=None, description="optional 校历 sheet listing every date in the period"
    )


# ==========================================================================
# Sheet reading
# ==========================================================================
@dataclass(frozen=True, slots=True)
class Row:
    """One spreadsheet row with the 1-based number the user sees."""

    number: int
    values: list[Any]


@dataclass(frozen=True, slots=True)
class SheetTable:
    """A sheet sliced per its :class:`SheetMap`: the header row plus data rows."""

    sheet: str
    header_row: int
    header: list[Any]
    rows: list[Row]


@dataclass(frozen=True, slots=True)
class ColumnBinding:
    """A resolved mapping entry: schema field, the user's column, its 0-based index."""

    field: str
    column: str | int
    index: int


def read_sheets(path: str | Path) -> dict[str, list[Row]]:
    """Read every sheet of a workbook -- or the single table of a CSV -- as raw rows.

    Cell values are returned as openpyxl/csv produced them (dates stay dates,
    numbers stay numbers) so that coercion can do its job.  Trailing all-empty
    rows are dropped.
    """
    target = Path(path)
    if target.suffix.lower() == ".csv":
        rows = [
            Row(number=index + 1, values=list(row))
            for index, row in enumerate(read_csv_table(target))
        ]
        return {target.stem: _trim_rows(rows)}
    workbook = load_workbook(target, read_only=True, data_only=True)
    try:
        return {
            sheet.title: _trim_rows(
                [
                    Row(number=index + 1, values=[cell for cell in values])
                    for index, values in enumerate(sheet.iter_rows(values_only=True))
                ]
            )
            for sheet in workbook.worksheets
        }
    finally:
        workbook.close()


def _trim_rows(rows: list[Row]) -> list[Row]:
    while rows and all(_is_blank(value) for value in rows[-1].values):
        rows.pop()
    return rows


def _as_sheet_map(mapping: SheetMap | Mapping[str, Any]) -> SheetMap:
    return mapping if isinstance(mapping, SheetMap) else SheetMap.model_validate(mapping)


def _table_for(
    sheets: Mapping[str, list[Row]], mapping: SheetMap, *, problems: list[str]
) -> SheetTable | None:
    """Locate the sheet, honour ``header_row``/``skip_rows``, and report what is wrong."""
    if mapping.sheet in sheets:
        raw = sheets[mapping.sheet]
    elif len(sheets) == 1:
        raw = next(iter(sheets.values()))  # a CSV: `sheet` is a label, not a lookup key
    else:
        names = "、".join(f"「{name}」" for name in sheets) or "（无）"
        problems.append(f"工作表「{mapping.sheet}」不存在；文件中的工作表：{names}")
        return None
    if mapping.header_row > len(raw):
        problems.append(
            f"工作表「{mapping.sheet}」只有 {len(raw)} 行，找不到第 {mapping.header_row} 行表头"
        )
        return None
    header = list(raw[mapping.header_row - 1].values)
    first_data = mapping.header_row + mapping.skip_rows
    return SheetTable(
        sheet=mapping.sheet,
        header_row=mapping.header_row,
        header=header,
        rows=raw[first_data:],
    )


def _header_key(value: Any) -> str:
    return re.sub(r"\s+", "", clean_text(value)).casefold()


def _column_index(table: SheetTable, column: str | int) -> int | None:
    if isinstance(column, int):
        return column
    target = _header_key(column)
    if not target:
        return None
    keys = [_header_key(cell) for cell in table.header]
    for index, key in enumerate(keys):
        if key == target:
            return index
    for index, key in enumerate(keys):  # tolerate "工号*", "姓名（必填）"
        if key.startswith(target):
            return index
    return None


def _header_preview(header: Sequence[Any], limit: int = 12) -> str:
    names = [clean_text(cell) or "（空）" for cell in header[:limit]]
    if len(header) > limit:
        names.append("…")
    return "、".join(names) if names else "（表头为空）"


def _resolve_columns(
    table: SheetTable,
    mapping: SheetMap,
    *,
    label: str,
    allowed: frozenset[str],
    aliases: Mapping[str, str],
    problems: list[str],
    allow_attributes: bool = False,
) -> dict[str, ColumnBinding]:
    """Bind every declared field to a column index, reporting mapping mistakes."""
    bindings: dict[str, ColumnBinding] = {}
    for column_map in mapping.columns:
        declared = column_map.field.strip()
        field = aliases.get(declared, declared)
        is_attribute = allow_attributes and field.startswith("attributes.") and len(field) > 11
        if field not in allowed and not is_attribute:
            hint = "，自定义属性请写成 attributes.部门" if allow_attributes else ""
            problems.append(
                f"工作表「{mapping.sheet}」的映射字段 {declared!r} 不是{label}支持的字段；"
                f"可用字段：{'、'.join(sorted(allowed))}{hint}"
            )
            continue
        index = _column_index(table, column_map.column)
        if index is None:
            problems.append(
                f"工作表「{mapping.sheet}」第 {mapping.header_row} 行找不到列"
                f"「{column_map.column}」（字段 {field}）；"
                f"该行实际列名：{_header_preview(table.header)}"
            )
            continue
        if field in bindings:
            problems.append(
                f"工作表「{mapping.sheet}」的字段 {field} 被映射到多列"
                f"（「{bindings[field].column}」和「{column_map.column}」），已采用前者"
            )
            continue
        bindings[field] = ColumnBinding(field=field, column=column_map.column, index=index)
    return bindings


def _problem(
    *,
    sheet: str,
    row: int | None,
    column: str | int | None,
    field: str | None,
    reason: str,
    person: str | None = None,
) -> str:
    """The one message format the whole importer uses: sheet, row, column, field, person."""
    where = f"工作表「{sheet}」"
    if row is not None:
        where += f" 第 {row} 行"
    if column is not None:
        where += f"，列「{column}」"
    if field:
        where += f"（字段 {field}）"
    if person:
        where += f"，人员={person}"
    return f"{where}：{reason}"


def _row_problem(
    mapping: SheetMap,
    row: Row,
    binding: ColumnBinding | str,
    reason: str,
    *,
    person: str | None = None,
    field: str | None = None,
) -> str:
    if isinstance(binding, ColumnBinding):
        column, name = binding.column, binding.field
    else:
        column, name = "", binding
    return _problem(
        sheet=mapping.sheet,
        row=row.number,
        column=column or None,
        field=field or name or None,
        reason=reason,
        person=person,
    )


def _get(row: Row, binding: ColumnBinding) -> Any:
    return row.values[binding.index] if binding.index < len(row.values) else None


def _validate(cls: type[TModel], data: dict[str, Any]) -> TModel:
    """Validate one row's data; pydantic's complaint is re-raised as a CoercionError."""
    try:
        return cls.model_validate(data)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(part) for part in first["loc"]) or cls.__name__
        raise CoercionError(f"字段 {location}：{first['msg']}") from None


def _rows_in_table(table: SheetTable) -> Iterable[Row]:
    for row in table.rows:
        if all(_is_blank(value) for value in row.values):
            continue
        yield row


def _read(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    sheets: Mapping[str, list[Row]] | None,
    problems: list[str],
) -> tuple[SheetMap, SheetTable | None]:
    sheet_map = _as_sheet_map(mapping)
    table = _table_for(
        sheets if sheets is not None else read_sheets(path), sheet_map, problems=problems
    )
    return sheet_map, table


# ==========================================================================
# Staff
# ==========================================================================
_STAFF_FIELDS = frozenset(
    {
        "id",
        "name",
        "role",
        "skills",
        "fte",
        "max_shifts",
        "min_shifts",
        "target_shifts",
        "history",
        "unavailable",
    }
)
_STAFF_ALIASES: dict[str, str] = {
    "person": "id",
    "employee": "id",
    "staff": "id",
    "person_id": "id",
    "staff_id": "id",
    "employee_id": "id",
    "job": "role",
    "position": "role",
    "title": "role",
    "skill": "skills",
    "workload": "fte",
    "shift_cap": "max_shifts",
    "max_shifts_over_horizon": "max_shifts",
}


def load_staff(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    default_role: str = "",
    default_fte: float = 1.0,
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[Person]:
    """Read a staff sheet into :class:`Person` objects.

    ``history`` cells look like ``2025-02-28:N1`` and ``unavailable`` cells like
    ``2025-03-05`` or ``2025-03-05:N1`` (``、``-separated).  Free-form columns can
    be mapped to ``attributes.<key>`` and land in ``Person.attributes``.
    """
    problems: list[str] = []
    sheet_map, table = _read(path, mapping, sheets=sheets, problems=problems)
    people: Loaded[Person] = Loaded(problems=problems)
    if table is None:
        return people
    bindings = _resolve_columns(
        table,
        sheet_map,
        label="人员表",
        allowed=_STAFF_FIELDS,
        aliases=_STAFF_ALIASES,
        allow_attributes=True,
        problems=problems,
    )
    if "id" not in bindings:
        problems.append(
            f"工作表「{sheet_map.sheet}」的人员表没有映射到人员编号字段（id）；"
            f"可用字段：{'、'.join(sorted(_STAFF_FIELDS))}"
        )
        return people

    seen: dict[str, int] = {}
    for row in _rows_in_table(table):
        raw = {field: _get(row, binding) for field, binding in bindings.items()}
        person_id = clean_text(raw["id"])
        if not person_id:
            problems.append(
                _row_problem(sheet_map, row, bindings["id"], "工号为空，无法确定人员，该行已跳过")
            )
            continue
        if person_id in seen:
            problems.append(
                _row_problem(
                    sheet_map,
                    row,
                    bindings["id"],
                    f"工号 {person_id!r} 与第 {seen[person_id]} 行重复，该行已跳过",
                )
            )
            continue

        data: dict[str, Any] = {
            "id": person_id,
            "name": clean_text(raw.get("name")) or person_id,
            "role": clean_text(raw.get("role")) or default_role,
            "skills": parse_list(raw.get("skills")),
            "attributes": {
                field.split(".", 1)[1]: clean_text(raw[field])
                for field in bindings
                if field.startswith("attributes.") and clean_text(raw[field])
            },
            "fte": default_fte,
        }
        for field in ("max_shifts", "min_shifts", "target_shifts"):
            binding = bindings.get(field)
            if binding is None or _is_blank(raw.get(field)):
                continue
            try:
                data[field] = parse_int(raw[field])
            except CoercionError as exc:
                problems.append(
                    _row_problem(sheet_map, row, binding, f"{exc}（已忽略该值）", person=person_id)
                )
        fte_binding = bindings.get("fte")
        if fte_binding is not None and not _is_blank(raw.get("fte")):
            try:
                data["fte"] = parse_float(raw["fte"])
            except CoercionError as exc:
                problems.append(
                    _row_problem(
                        sheet_map,
                        row,
                        fte_binding,
                        f"{exc}（已改用默认值 {default_fte}）",
                        person=person_id,
                    )
                )

        if (binding := bindings.get("history")) is not None:
            parsed_history: list[HistoryEntry] = []
            for token in parse_list(raw.get("history")):
                date_text, _, shift_id = token.partition(":")
                try:
                    entry_date = parse_date(date_text)
                except CoercionError as exc:
                    problems.append(
                        _row_problem(
                            sheet_map,
                            row,
                            binding,
                            f"历史班次 {token!r} 的日期无法识别：{exc}",
                            person=person_id,
                        )
                    )
                    continue
                if not shift_id.strip():
                    problems.append(
                        _row_problem(
                            sheet_map,
                            row,
                            binding,
                            f"历史班次 {token!r} 缺少班次号（应写成 2025-02-28:N1）",
                            person=person_id,
                        )
                    )
                    continue
                parsed_history.append(HistoryEntry(date=entry_date, shift=shift_id.strip()))
            data["history"] = parsed_history

        if (binding := bindings.get("unavailable")) is not None:
            unavailable: list[str] = []
            for token in parse_list(raw.get("unavailable")):
                date_text, _, shift_id = token.partition(":")
                try:
                    unavailable_date = parse_date(date_text)
                except CoercionError as exc:
                    problems.append(
                        _row_problem(
                            sheet_map,
                            row,
                            binding,
                            f"不可用日期 {token!r} 无法识别：{exc}",
                            person=person_id,
                        )
                    )
                    continue
                suffix = f":{shift_id.strip()}" if shift_id.strip() else ""
                unavailable.append(f"{unavailable_date.isoformat()}{suffix}")
            data["unavailable"] = unavailable

        try:
            person = _validate(Person, data)
        except CoercionError as exc:
            problems.append(
                _row_problem(sheet_map, row, bindings["id"], f"{exc}，该行已跳过", person=person_id)
            )
            continue
        seen[person_id] = row.number
        people.append(person)
    return people


# ==========================================================================
# Shifts
# ==========================================================================
_SHIFT_FIELDS = frozenset({"id", "name", "start", "end", "hours", "attendance"})
_SHIFT_ALIASES: dict[str, str] = {
    "shift_id": "id",
    "code": "id",
    "shift": "id",
    "label": "name",
    "start_time": "start",
    "end_time": "end",
    "from": "start",
    "to": "end",
    "work_hours": "hours",
    "duration": "hours",
    "counts": "attendance",
}


def load_shifts(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[Shift]:
    """Read a shift catalogue.  ``start``/``end`` are normalised to ``'HH:MM'``."""
    problems: list[str] = []
    sheet_map, table = _read(path, mapping, sheets=sheets, problems=problems)
    shifts: Loaded[Shift] = Loaded(problems=problems)
    if table is None:
        return shifts
    bindings = _resolve_columns(
        table,
        sheet_map,
        label="班次表",
        allowed=_SHIFT_FIELDS,
        aliases=_SHIFT_ALIASES,
        problems=problems,
    )
    required = [field for field in ("id", "start", "end") if field not in bindings]
    if required:
        problems.append(
            f"工作表「{sheet_map.sheet}」的班次表缺少必需字段：{'、'.join(required)}"
            f"（字段名：{'、'.join(sorted(_SHIFT_FIELDS))}）"
        )
        return shifts

    seen: dict[str, int] = {}
    for row in _rows_in_table(table):
        shift_id = clean_text(_get(row, bindings["id"]))
        data: dict[str, Any] = {
            "id": shift_id,
            "name": clean_text(_get(row, bindings["name"])) if "name" in bindings else "",
        }
        if not shift_id:
            problems.append(_row_problem(sheet_map, row, bindings["id"], "班次号为空，该行已跳过"))
            continue
        if shift_id in seen:
            problems.append(
                _row_problem(
                    sheet_map,
                    row,
                    bindings["id"],
                    f"班次号 {shift_id!r} 与第 {seen[shift_id]} 行重复，该行已跳过",
                )
            )
            continue
        failed = False
        for field in ("start", "end"):
            try:
                data[field] = format_time(parse_time(_get(row, bindings[field])))
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, bindings[field], str(exc)))
                failed = True
        if failed:
            continue
        if (binding := bindings.get("hours")) is not None and not _is_blank(_get(row, binding)):
            try:
                data["hours"] = parse_float(_get(row, binding))
            except CoercionError as exc:
                problems.append(
                    _row_problem(sheet_map, row, binding, f"{exc}（已改为按起止时间推算）")
                )
        if (binding := bindings.get("attendance")) is not None and not _is_blank(
            _get(row, binding)
        ):
            try:
                data["attendance"] = parse_bool(_get(row, binding))
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, binding, f"{exc}（已按出勤处理）"))
        try:
            shift = _validate(Shift, data)
        except CoercionError as exc:
            problems.append(_row_problem(sheet_map, row, bindings["id"], f"{exc}，该行已跳过"))
            continue
        seen[shift_id] = row.number
        shifts.append(shift)
    return shifts


# ==========================================================================
# Demand
# ==========================================================================
_DEMAND_FIELDS = frozenset({"shift", "date", "weekday", "min", "target", "max", "note"})
_DEMAND_ALIASES: dict[str, str] = {
    "shift_id": "shift",
    "minimum": "min",
    "min_count": "min",
    "min_people": "min",
    "maximum": "max",
    "max_count": "max",
    "max_people": "max",
    "target_count": "target",
    "target_people": "target",
    "remark": "note",
}


def _date_or_weekday(value: Any) -> tuple[dt.date | None, Weekday | None]:
    """A 日期 column may hold a real date *or* ``周一`` -- both are natural to a user."""
    try:
        return parse_date(value), None
    except CoercionError as date_error:
        try:
            return None, parse_weekday(value)
        except CoercionError:
            raise CoercionError(f"{date_error}；也无法识别为星期（请写成 周一 ~ 周日）") from None


def load_demand(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[DemandRule]:
    """Read coverage requirements; each row needs exactly one of 日期/星期.

    A 日期 cell may hold a single date, a weekday (``周一``), or a **range**
    (``2025-03-01~2025-03-05``).  ``DemandRule`` can only express one date per
    rule, so a range is expanded into one rule per day -- which is what the
    教务员 means by "这一周每天都要 2 个人".
    """
    problems: list[str] = []
    sheet_map, table = _read(path, mapping, sheets=sheets, problems=problems)
    rules: Loaded[DemandRule] = Loaded(problems=problems)
    if table is None:
        return rules
    bindings = _resolve_columns(
        table,
        sheet_map,
        label="需求表",
        allowed=_DEMAND_FIELDS,
        aliases=_DEMAND_ALIASES,
        problems=problems,
    )
    if "shift" not in bindings:
        problems.append(
            f"工作表「{sheet_map.sheet}」的需求表没有映射到班次字段（shift）；"
            f"可用字段：{'、'.join(sorted(_DEMAND_FIELDS))}"
        )
        return rules

    for row in _rows_in_table(table):
        shift_id = clean_text(_get(row, bindings["shift"]))
        if not shift_id:
            problems.append(
                _row_problem(sheet_map, row, bindings["shift"], "班次号为空，该行已跳过")
            )
            continue
        data: dict[str, Any] = {"shift": shift_id, "min": 0}
        expanded_dates: list[dt.date] = []
        explicit_weekday: Weekday | None = None
        if (binding := bindings.get("weekday")) is not None and not _is_blank(_get(row, binding)):
            try:
                explicit_weekday = parse_weekday(_get(row, binding))
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, binding, str(exc), person=None))
                continue
        if (binding := bindings.get("date")) is not None and not _is_blank(_get(row, binding)):
            cell = clean_text(_get(row, binding))
            span = _split_range(cell)
            if span is not None:
                try:
                    first, last = parse_date(span[0]), parse_date(span[1])
                except CoercionError as exc:
                    problems.append(
                        _row_problem(sheet_map, row, binding, f"日期区间无法识别：{exc}")
                    )
                    continue
                if last < first:
                    problems.append(
                        _row_problem(
                            sheet_map,
                            row,
                            binding,
                            f"区间结束 {last} 早于开始 {first}，该行已跳过",
                        )
                    )
                    continue
                if explicit_weekday is not None:
                    problems.append(
                        _row_problem(
                            sheet_map,
                            row,
                            binding,
                            "同时给了日期区间和星期，无法判断适用哪些天，该行已跳过",
                        )
                    )
                    continue
                expanded_dates = [
                    first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)
                ]
            else:
                try:
                    explicit_date, weekday_from_date = _date_or_weekday(cell)
                except CoercionError as exc:
                    problems.append(_row_problem(sheet_map, row, binding, str(exc)))
                    continue
                if explicit_date is not None and explicit_weekday is not None:
                    problems.append(
                        _row_problem(
                            sheet_map,
                            row,
                            binding,
                            "同时给了具体日期和星期，无法判断适用哪一天，该行已跳过",
                        )
                    )
                    continue
                if explicit_date is not None:
                    data["date"] = explicit_date
                else:
                    explicit_weekday = weekday_from_date
        if explicit_weekday is not None:
            data["weekday"] = explicit_weekday
        if not expanded_dates and "date" not in data and "weekday" not in data:
            problems.append(
                f"工作表「{sheet_map.sheet}」 第 {row.number} 行，班次 {shift_id!r}："
                f"既没有日期也没有星期，无法判断需求适用于哪一天，该行已跳过"
            )
            continue
        for field in ("min", "target", "max"):
            binding = bindings.get(field)
            if binding is None or _is_blank(_get(row, binding)):
                continue
            try:
                data[field] = parse_int(_get(row, binding))
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, binding, str(exc)))
                continue
            if field == "target" and "min" in data and data[field] < data["min"]:
                problems.append(
                    _row_problem(
                        sheet_map,
                        row,
                        binding,
                        f"目标人数 {data[field]} 小于最小人数 {data['min']}",
                    )
                )
            if field == "max" and "min" in data and data[field] < data["min"]:
                problems.append(
                    _row_problem(
                        sheet_map,
                        row,
                        binding,
                        f"最大人数 {data[field]} 小于最小人数 {data['min']}",
                    )
                )
        if (binding := bindings.get("note")) is not None:
            data["note"] = clean_text(_get(row, binding))
        # one validated rule per expanded day (or a single rule for a date/weekday row)
        targets: list[dt.date | None] = [*expanded_dates] if expanded_dates else [None]
        for expanded in targets:
            payload = dict(data)
            if expanded is not None:
                payload.pop("weekday", None)
                payload["date"] = expanded
            try:
                rules.append(_validate(DemandRule, payload))
            except CoercionError as exc:
                problems.append(
                    f"工作表「{sheet_map.sheet}」 第 {row.number} 行，班次 {shift_id!r}："
                    f"{exc}，该行已跳过"
                )
    return rules


# ==========================================================================
# Unavailability
# ==========================================================================
_UNAVAILABILITY_FIELDS = frozenset({"person", "date", "dates", "start", "end", "shifts", "reason"})
_UNAVAILABILITY_ALIASES: dict[str, str] = {
    "employee": "person",
    "staff": "person",
    "person_id": "person",
    "employee_id": "person",
    "shift": "shifts",
    "period": "dates",
    "date_range": "dates",
    "range": "dates",
    "from": "start",
    "to": "end",
    "remark": "reason",
    "note": "reason",
}
_RANGE_SEPARATORS = ("~", "～", "至", "到", "--")


def _split_range(text: str) -> tuple[str, str] | None:
    for separator in _RANGE_SEPARATORS:
        if separator in text:
            left, _, right = text.partition(separator)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
    return None


def load_unavailability(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[UnavailabilityRule]:
    """Read approved absences.  A ``dates`` cell may hold ``2025-03-01~2025-03-05``."""
    problems: list[str] = []
    sheet_map, table = _read(path, mapping, sheets=sheets, problems=problems)
    rules: Loaded[UnavailabilityRule] = Loaded(problems=problems)
    if table is None:
        return rules
    bindings = _resolve_columns(
        table,
        sheet_map,
        label="不可用表",
        allowed=_UNAVAILABILITY_FIELDS,
        aliases=_UNAVAILABILITY_ALIASES,
        problems=problems,
    )
    if "person" not in bindings:
        problems.append(
            f"工作表「{sheet_map.sheet}」的不可用表没有映射到人员字段（person）；"
            f"可用字段：{'、'.join(sorted(_UNAVAILABILITY_FIELDS))}"
        )
        return rules

    for row in _rows_in_table(table):
        person_id = clean_text(_get(row, bindings["person"]))
        if not person_id:
            problems.append(
                _row_problem(sheet_map, row, bindings["person"], "人员为空，该行已跳过")
            )
            continue
        data: dict[str, Any] = {"person": person_id, "shifts": [], "reason": ""}
        dates: list[dt.date] = []
        date_range: tuple[dt.date, dt.date] | None = None
        broken = False

        start_binding, end_binding = bindings.get("start"), bindings.get("end")
        if start_binding is not None and not _is_blank(_get(row, start_binding)):
            try:
                first = parse_date(_get(row, start_binding))
                last = (
                    parse_date(_get(row, end_binding))
                    if end_binding is not None and not _is_blank(_get(row, end_binding))
                    else first
                )
            except CoercionError as exc:
                problems.append(
                    _row_problem(sheet_map, row, start_binding, str(exc), person=person_id)
                )
                broken = True
            else:
                date_range = (first, last)
        for name in ("dates", "date"):
            binding = bindings.get(name)
            if binding is None or _is_blank(_get(row, binding)):
                continue
            cell = clean_text(_get(row, binding))
            span = _split_range(cell)
            if span is not None:
                try:
                    date_range = (parse_date(span[0]), parse_date(span[1]))
                except CoercionError as exc:
                    problems.append(
                        _row_problem(
                            sheet_map, row, binding, f"日期区间无法识别：{exc}", person=person_id
                        )
                    )
                    broken = True
            else:
                for token in parse_list(cell):
                    try:
                        dates.append(parse_date(token))
                    except CoercionError as exc:
                        problems.append(
                            _row_problem(sheet_map, row, binding, str(exc), person=person_id)
                        )
                        broken = True
        if broken:
            continue
        if not dates and date_range is None:
            problems.append(
                f"工作表「{sheet_map.sheet}」 第 {row.number} 行，人员={person_id}："
                f"既没有日期也没有日期区间，该行已跳过"
            )
            continue
        data["dates"] = sorted(set(dates))
        if date_range is not None:
            if date_range[1] < date_range[0]:
                problems.append(
                    f"工作表「{sheet_map.sheet}」 第 {row.number} 行，人员={person_id}："
                    f"区间结束 {date_range[1]} 早于开始 {date_range[0]}，该行已跳过"
                )
                continue
            data["date_range"] = date_range
        if (binding := bindings.get("shifts")) is not None:
            data["shifts"] = parse_list(_get(row, binding))
        if (binding := bindings.get("reason")) is not None:
            data["reason"] = clean_text(_get(row, binding))
        try:
            rules.append(_validate(UnavailabilityRule, data))
        except CoercionError as exc:
            problems.append(
                f"工作表「{sheet_map.sheet}」 第 {row.number} 行，人员={person_id}："
                f"{exc}，该行已跳过"
            )
    return rules


# ==========================================================================
# Preferences
# ==========================================================================
_PREFERENCE_FIELDS = frozenset({"person", "shift", "kind", "date", "weekday", "weight", "note"})
_PREFERENCE_ALIASES: dict[str, str] = {
    "employee": "person",
    "staff": "person",
    "person_id": "person",
    "employee_id": "person",
    "shift_id": "shift",
    "type": "kind",
    "request": "kind",
    "preference": "kind",
    "direction": "kind",
    "remark": "note",
}
_WANT_TOKENS = frozenset(
    {"want", "wish", "希望", "想要", "想", "要", "申请", "偏好", "1", "y", "yes", "true"}
)
_AVOID_TOKENS = frozenset({"avoid", "no", "不要", "不想", "避免", "避开", "不", "0", "n", "false"})


def _parse_kind(value: Any, default_kind: PreferenceKind) -> PreferenceKind:
    if _is_blank(value):
        return default_kind
    text = clean_text(value).casefold()
    if text in _WANT_TOKENS:
        return "want"
    if text in _AVOID_TOKENS:
        return "avoid"
    raise CoercionError(
        f"无法把 {clean_text(value)!r} 识别为偏好类型（请写 想要 / 不想，或 want / avoid）"
    )


def load_preferences(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    default_kind: PreferenceKind = "want",
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[Preference]:
    """Read shift requests.  ``kind`` accepts 想要/不想 as well as want/avoid."""
    problems: list[str] = []
    sheet_map, table = _read(path, mapping, sheets=sheets, problems=problems)
    prefs: Loaded[Preference] = Loaded(problems=problems)
    if table is None:
        return prefs
    bindings = _resolve_columns(
        table,
        sheet_map,
        label="偏好表",
        allowed=_PREFERENCE_FIELDS,
        aliases=_PREFERENCE_ALIASES,
        problems=problems,
    )
    missing = [field for field in ("person", "shift") if field not in bindings]
    if missing:
        problems.append(
            f"工作表「{sheet_map.sheet}」的偏好表缺少必需字段：{'、'.join(missing)}"
            f"（字段名：{'、'.join(sorted(_PREFERENCE_FIELDS))}）"
        )
        return prefs

    for row in _rows_in_table(table):
        person_id = clean_text(_get(row, bindings["person"]))
        shift_id = clean_text(_get(row, bindings["shift"]))
        if not person_id or not shift_id:
            missing_field = "person" if not person_id else "shift"
            problems.append(
                _row_problem(
                    sheet_map,
                    row,
                    bindings[missing_field],
                    "人员或班次为空，该行已跳过",
                    person=person_id or None,
                )
            )
            continue
        data: dict[str, Any] = {"person": person_id, "shift": shift_id, "weight": 1, "note": ""}
        if (binding := bindings.get("kind")) is not None:
            try:
                data["kind"] = _parse_kind(_get(row, binding), default_kind)
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, binding, str(exc), person=person_id))
                continue
        else:
            data["kind"] = default_kind
        explicit_weekday: Weekday | None = None
        if (binding := bindings.get("weekday")) is not None and not _is_blank(_get(row, binding)):
            try:
                explicit_weekday = parse_weekday(_get(row, binding))
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, binding, str(exc), person=person_id))
                continue
        if (binding := bindings.get("date")) is not None and not _is_blank(_get(row, binding)):
            try:
                explicit_date, weekday_from_date = _date_or_weekday(_get(row, binding))
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, binding, str(exc), person=person_id))
                continue
            if explicit_date is not None and explicit_weekday is not None:
                problems.append(
                    _row_problem(
                        sheet_map,
                        row,
                        binding,
                        "同时给了具体日期和星期，无法判断偏好适用于哪一天，该行已跳过",
                        person=person_id,
                    )
                )
                continue
            if explicit_date is not None:
                data["date"] = explicit_date
            else:
                explicit_weekday = weekday_from_date
        if explicit_weekday is not None:
            data["weekday"] = explicit_weekday
        if (binding := bindings.get("weight")) is not None and not _is_blank(_get(row, binding)):
            try:
                data["weight"] = parse_int(_get(row, binding))
            except CoercionError as exc:
                problems.append(
                    _row_problem(
                        sheet_map, row, binding, f"{exc}（已改用权重 1）", person=person_id
                    )
                )
        if (binding := bindings.get("note")) is not None:
            data["note"] = clean_text(_get(row, binding))
        try:
            prefs.append(_validate(Preference, data))
        except CoercionError as exc:
            problems.append(
                f"工作表「{sheet_map.sheet}」 第 {row.number} 行，人员={person_id}，"
                f"班次={shift_id}：{exc}，该行已跳过"
            )
    return prefs


# ==========================================================================
# Holidays & horizon
# ==========================================================================
_DATE_ONLY_FIELDS = frozenset({"date"})
_DATE_ONLY_ALIASES: dict[str, str] = {
    "holiday": "date",
    "日期": "date",
    "节假日": "date",
    "period": "date",
}


def load_holidays(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[dt.date]:
    """Read a holiday list (one date per row, or several ``、``-separated per row)."""
    return load_dates(path, mapping, label="节假日表", sheets=sheets)


def load_horizon(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[dt.date]:
    """Read a 校历 sheet listing every date in the scheduling period."""
    return load_dates(path, mapping, label="校历表", sheets=sheets)


def load_dates(
    path: str | Path,
    mapping: SheetMap | Mapping[str, Any],
    *,
    label: str,
    sheets: Mapping[str, list[Row]] | None = None,
) -> Loaded[dt.date]:
    """Shared reader for one-date-per-row sheets."""
    problems: list[str] = []
    sheet_map, table = _read(path, mapping, sheets=sheets, problems=problems)
    dates: Loaded[dt.date] = Loaded(problems=problems)
    if table is None:
        return dates
    bindings = _resolve_columns(
        table,
        sheet_map,
        label=label,
        allowed=_DATE_ONLY_FIELDS,
        aliases=_DATE_ONLY_ALIASES,
        problems=problems,
    )
    if "date" not in bindings:
        problems.append(f"工作表「{sheet_map.sheet}」的{label}没有映射到日期字段（date）")
        return dates
    binding = bindings["date"]
    for row in _rows_in_table(table):
        for token in parse_list(_get(row, binding)) or [""]:
            try:
                dates.append(parse_date(token))
            except CoercionError as exc:
                problems.append(_row_problem(sheet_map, row, binding, str(exc)))
    return dates


# ==========================================================================
# Whole-instance import
# ==========================================================================
class InstanceParts(BaseModel):
    """The parts of an :class:`~scheduling_or.schema.instance.Instance` an import can supply.

    ``groups`` is deliberately absent: a group is *policy* (which sets of people a
    constraint may draw from), so it belongs in the spec the interview produces,
    never in the 教务员's spreadsheet.
    """

    model_config = ConfigDict(extra="forbid")

    horizon: Horizon | None = None
    shifts: list[Shift] | None = None
    people: list[Person] | None = None
    demand: list[DemandRule] | None = None
    unavailability: list[UnavailabilityRule] | None = None
    preferences: list[Preference] | None = None
    holidays: list[dt.date] | None = None

    def kwargs(self) -> dict[str, Any]:
        """Only the parts that were found, ready to merge with config/spec-supplied facts."""
        return self.model_dump(exclude_none=True)


class ImportResult(BaseModel):
    """``{instance_parts, problems}`` -- what was read, and everything wrong with it."""

    model_config = ConfigDict(extra="forbid")

    instance_parts: InstanceParts = Field(default_factory=InstanceParts)
    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing at all went wrong (an empty sheet is not a problem *string*)."""
        return not self.problems

    def instance_kwargs(self) -> dict[str, Any]:
        """Shorthand for ``instance_parts.kwargs()``."""
        return self.instance_parts.kwargs()


def import_instance(
    path: str | Path,
    workbook_map: WorkbookMap | Mapping[str, Any],
    *,
    default_role: str = "",
    default_fte: float = 1.0,
    default_kind: PreferenceKind = "want",
) -> ImportResult:
    """Read one workbook into partial instance facts plus row-located problems.

    A CSV file is read as a single sheet: its ``SheetMap.sheet`` is a label only.
    A malformed *mapping* raises (a config error must be loud, per the schema
    discipline); malformed *cells* only ever become problem strings.
    """
    target = Path(path)
    mapping = (
        workbook_map
        if isinstance(workbook_map, WorkbookMap)
        else WorkbookMap.model_validate(workbook_map)
    )
    if not target.exists():
        return ImportResult(problems=[f"找不到文件：{target}"])

    sheets = read_sheets(target)
    problems: list[str] = []
    parts = InstanceParts()

    if mapping.shifts is not None:
        shifts = load_shifts(target, mapping.shifts, sheets=sheets)
        problems.extend(shifts.problems)
        if shifts:
            parts.shifts = list(shifts)
    if mapping.staff is not None:
        people = load_staff(
            target, mapping.staff, default_role=default_role, default_fte=default_fte, sheets=sheets
        )
        problems.extend(people.problems)
        if people:
            parts.people = list(people)
    if mapping.demand is not None:
        demand = load_demand(target, mapping.demand, sheets=sheets)
        problems.extend(demand.problems)
        if demand:
            parts.demand = list(demand)
    if mapping.unavailability is not None:
        absence = load_unavailability(target, mapping.unavailability, sheets=sheets)
        problems.extend(absence.problems)
        if absence:
            parts.unavailability = list(absence)
    if mapping.preferences is not None:
        preferences = load_preferences(
            target, mapping.preferences, default_kind=default_kind, sheets=sheets
        )
        problems.extend(preferences.problems)
        if preferences:
            parts.preferences = list(preferences)
    if mapping.holidays is not None:
        holidays = load_holidays(target, mapping.holidays, sheets=sheets)
        problems.extend(holidays.problems)
        if holidays:
            parts.holidays = sorted(set(holidays))

    if mapping.horizon is not None:
        horizon_dates = load_horizon(target, mapping.horizon, sheets=sheets)
        problems.extend(horizon_dates.problems)
        if horizon_dates:
            parts.horizon = Horizon(start=min(horizon_dates), end=max(horizon_dates))
    else:
        parts.horizon = _infer_horizon(parts)

    problems.extend(_cross_check(parts))
    return ImportResult(instance_parts=parts, problems=problems)


def _infer_horizon(parts: InstanceParts) -> Horizon | None:
    """Infer the period from explicit dates; the caller must record this as an assumption."""
    dates: list[dt.date] = list(parts.holidays or [])
    dates.extend(rule.date for rule in parts.demand or [] if rule.date is not None)
    for rule in parts.unavailability or []:
        dates.extend(rule.resolved_dates())
    dates.extend(pref.date for pref in parts.preferences or [] if pref.date is not None)
    if not dates:
        return None
    return Horizon(start=min(dates), end=max(dates))


def _cross_check(parts: InstanceParts) -> list[str]:
    """Reference/duplication problems the per-sheet loaders cannot see.

    ``lint.py`` owns the full data-quality pass; these few checks exist so that an
    import never hands the kernel a dangling 班次号 without saying so.
    """
    problems: list[str] = []
    shift_ids = {shift.id for shift in parts.shifts or []}
    person_ids = {person.id for person in parts.people or []}
    if parts.shifts:
        for demand_rule in parts.demand or []:
            if demand_rule.shift not in shift_ids:
                problems.append(f"需求表引用了班次表中不存在的班次 {demand_rule.shift!r}")
        for absent in parts.unavailability or []:
            for shift_id in absent.shifts:
                if shift_id not in shift_ids:
                    problems.append(
                        f"不可用表（人员={absent.person}）引用了不存在的班次 {shift_id!r}"
                    )
    if parts.people:
        for absent in parts.unavailability or []:
            if absent.person not in person_ids:
                problems.append(f"不可用表引用了人员表中不存在的人员 {absent.person!r}")
        for pref in parts.preferences or []:
            if pref.person not in person_ids:
                problems.append(f"偏好表引用了人员表中不存在的人员 {pref.person!r}")
    if parts.shifts and parts.preferences:
        for pref in parts.preferences:
            if pref.shift not in shift_ids:
                problems.append(f"偏好表（人员={pref.person}）引用了不存在的班次 {pref.shift!r}")
    return problems
