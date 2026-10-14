# (This module deliberately emits Chinese text: full-width punctuation here is
#  user-facing output, never a confusable-character typo.)
"""Solution -> workbook: a wide monthly grid *and* a tidy long sheet, always both.

Three rules from ``docs/plans`` §11 shape this module:

1. **Two views, never one.**  ``Roster`` (person x date, cell = shift name) is the
   only human-readable form a 教务员 can use; ``Assignments`` (one row per
   person-date-shift) is what machines and other systems consume.  ``Coverage``,
   ``Fairness`` and ``Violations`` answer the three questions asked of any roster
   ("is every slot filled?", "is the load fair?", "what did we trade away?").
2. **New files use XlsxWriter, existing user templates use openpyxl.**  A workbook
   is never routed through pandas: ``to_excel`` over an existing file destroys the
   user's sheets, formulas and styles.
3. **Colour-code shifts deterministically.**  :func:`shift_colour` hashes the shift
   id, so 早班 is the same colour in every sheet and every run -- unlike ``hash()``,
   which is salted per process and would repaint the roster on each run.

Layout and styling live in exactly one place (:func:`_write_roster_grid`,
:func:`_write_table`, :func:`_render_sheets`) and are rendered through a small
backend adapter, so the XlsxWriter path and the openpyxl template path cannot
drift apart.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import xlsxwriter
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from ..schema.instance import Instance
from ..schema.solution import Solution
from ..schema.spec import Spec
from . import csv_io
from .csv_io import Table, weekday_cn

__all__ = [
    "SHIFT_PALETTE",
    "Layout",
    "export_csv",
    "export_into_template",
    "export_workbook",
    "shift_colour",
]

Layout = Literal["monthly", "long", "both"]
_LAYOUTS: tuple[str, ...] = ("monthly", "long", "both")

SHEET_README = "README"
SHEET_ROSTER = "Roster"
SHEET_ASSIGNMENTS = "Assignments"
SHEET_COVERAGE = "Coverage"
SHEET_FAIRNESS = "Fairness"
SHEET_VIOLATIONS = "Violations"
OWNED_SHEETS: tuple[str, ...] = (
    SHEET_README,
    SHEET_ROSTER,
    SHEET_ASSIGNMENTS,
    SHEET_COVERAGE,
    SHEET_FAIRNESS,
    SHEET_VIOLATIONS,
)

SHIFT_PALETTE: tuple[str, ...] = (
    "#FDE68A",
    "#BFDBFE",
    "#BBF7D0",
    "#FBCFE8",
    "#DDD6FE",
    "#FED7AA",
    "#A5F3FC",
    "#F5D0FE",
    "#D9F99D",
    "#FECACA",
    "#C7D2FE",
    "#99F6E4",
)
"""Light fills that stay readable under dark text and survive printing in greyscale.

More shifts than colours is normal: two shifts may share a colour (the README
legend lists them explicitly), but a given shift id never changes colour.
"""

_ANNOTATION_NOTE = (
    "红字 = 未经独立校验或存在冲突（详见 Violations）；灰底 = 周末；蓝底 = 节假日；"
    "同一班次在本工作簿内颜色一致。CSV 文件没有颜色标注。"
)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CJK = re.compile(r"[\u2e80-\u9fff\uff00-\uffef]")


def shift_colour(shift_id: str) -> str:
    """Stable fill colour for a shift id: same shift, same colour everywhere."""
    digest = hashlib.sha1(shift_id.encode("utf-8")).digest()
    return SHIFT_PALETTE[int.from_bytes(digest[:4], "big") % len(SHIFT_PALETTE)]


def _check_layout(layout: str) -> Layout:
    if layout not in _LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; expected one of {', '.join(_LAYOUTS)}")
    return cast(Layout, layout)


# ==========================================================================
# Styling: one description, two renderers
# ==========================================================================
@dataclass(frozen=True, slots=True)
class StyleSpec:
    """One visual style, described once and rendered by either backend."""

    fill: str | None = None
    font: str = "#1F1F1F"
    bold: bool = False
    align: str | None = None
    border: bool = True
    wrap: bool = False
    num_format: str = "General"


_NUMBER = "0.0##"

_STYLES: dict[str, StyleSpec] = {
    "section": StyleSpec(fill="#DDEBF7", bold=True),
    "header": StyleSpec(fill="#1F4E79", font="#FFFFFF", bold=True, align="center", wrap=True),
    "week": StyleSpec(fill="#D9E2F3", bold=True, align="center"),
    "date": StyleSpec(align="center", wrap=True),
    "weekend": StyleSpec(fill="#D9D9D9", bold=True, align="center", wrap=True),
    "holiday": StyleSpec(fill="#BDD7EE", font="#C00000", bold=True, align="center", wrap=True),
    "weekend_blank": StyleSpec(fill="#F2F2F2"),
    "holiday_blank": StyleSpec(fill="#DEEBF7"),
    "label": StyleSpec(bold=True),
    "plain": StyleSpec(),
    "text": StyleSpec(wrap=True),
    "number": StyleSpec(num_format=_NUMBER),
    "total": StyleSpec(fill="#F2F2F2", bold=True),
    "bad": StyleSpec(fill="#FFC7CE", font="#9C0006", bold=True),
    "warn": StyleSpec(fill="#FFEB9C", font="#9C6500"),
    "flagged": StyleSpec(font="#9C0006", bold=True),
}


def _style_spec(key: str) -> StyleSpec:
    """Resolve a style key, including the per-shift ``shift:<fill>`` forms."""
    if key.startswith("shift_flagged:"):
        return StyleSpec(
            fill=key.split(":", 1)[1], font="#9C0006", bold=True, align="center", border=True
        )
    if key.startswith("shift:"):
        return StyleSpec(fill=key.split(":", 1)[1], align="center", border=True)
    return _STYLES[key]


def _display_width(value: Any) -> int:
    text = "" if value is None else str(value)
    return len(text) + len(_CJK.findall(text))  # CJK glyphs occupy two columns


def _column_widths(table: Table) -> list[int]:
    widths: list[int] = []
    for row in table:
        for index, value in enumerate(row):
            if index >= len(widths):
                widths.append(0)
            widths[index] = max(widths[index], _display_width(value))
    return widths


# ==========================================================================
# Backend adapters (0-based coordinates, same as XlsxWriter)
# ==========================================================================
class _SheetWriter(Protocol):
    """The writing primitives both backends must provide."""

    def write(self, row: int, column: int, value: Any, style: str) -> None: ...

    def merge(
        self,
        first_row: int,
        first_column: int,
        last_row: int,
        last_column: int,
        value: Any,
        style: str,
    ) -> None: ...

    def set_width(self, column: int, width: float) -> None: ...

    def set_row_height(self, row: int, height: float) -> None: ...

    def freeze(self, row: int, column: int) -> None: ...

    def print_setup(self, *, landscape: bool, repeat_rows: tuple[int, int] | None) -> None: ...

    def autofilter(self, first_row: int, last_row: int, last_column: int) -> None: ...


class _XlsxSheet:
    """XlsxWriter backend: fast and fully format-controllable, but new files only."""

    def __init__(self, worksheet: Any, workbook: Any, formats: dict[str, Any]) -> None:
        self.worksheet = worksheet
        self.workbook = workbook
        self._formats = formats

    def _format(self, style: str) -> Any:
        if style not in self._formats:
            spec = _style_spec(style)
            self._formats[style] = self.workbook.add_format(
                {
                    "bg_color": spec.fill or "#FFFFFF",
                    "font_color": spec.font,
                    "bold": spec.bold,
                    "align": spec.align or "left",
                    "valign": "vcenter",
                    "text_wrap": spec.wrap,
                    "border": 1 if spec.border else 0,
                    "border_color": "#BFBFBF",
                    "num_format": spec.num_format,
                }
            )
        return self._formats[style]

    def write(self, row: int, column: int, value: Any, style: str) -> None:
        if value is None:
            self.worksheet.write_blank(row, column, None, self._format(style))
        else:
            self.worksheet.write(row, column, value, self._format(style))

    def merge(
        self,
        first_row: int,
        first_column: int,
        last_row: int,
        last_column: int,
        value: Any,
        style: str,
    ) -> None:
        self.worksheet.merge_range(
            first_row, first_column, last_row, last_column, value, self._format(style)
        )

    def set_width(self, column: int, width: float) -> None:
        self.worksheet.set_column(column, column, width)

    def set_row_height(self, row: int, height: float) -> None:
        self.worksheet.set_row(row, height)

    def freeze(self, row: int, column: int) -> None:
        self.worksheet.freeze_panes(row, column)

    def print_setup(self, *, landscape: bool, repeat_rows: tuple[int, int] | None) -> None:
        if landscape:
            self.worksheet.set_landscape()  # §11.3: print settings -> LibreOffice PDF
        self.worksheet.set_paper(9)  # A4
        self.worksheet.fit_to_pages(1, 0)
        if repeat_rows is not None:
            self.worksheet.repeat_rows(*repeat_rows)

    def autofilter(self, first_row: int, last_row: int, last_column: int) -> None:
        if last_row > first_row:
            self.worksheet.autofilter(first_row, 0, last_row, last_column)


class _OpenpyxlSheet:
    """openpyxl backend: the only way to write into a workbook without losing it."""

    def __init__(self, worksheet: Any, cache: dict[str, dict[str, Any]]) -> None:
        self.worksheet = worksheet
        self._cache = cache

    def _style(self, style: str) -> dict[str, Any]:
        if style not in self._cache:
            spec = _style_spec(style)
            side = Side(style="thin", color="BFBFBF")
            self._cache[style] = {
                "font": Font(color=spec.font.lstrip("#"), bold=spec.bold),
                "fill": PatternFill(
                    fill_type="solid", start_color=(spec.fill or "#FFFFFF").lstrip("#")
                ),
                "alignment": Alignment(
                    horizontal=spec.align or "left", vertical="center", wrap_text=spec.wrap
                ),
                "border": (
                    Border(left=side, right=side, top=side, bottom=side) if spec.border else None
                ),
                "num_format": spec.num_format,
            }
        return self._cache[style]

    def _apply(self, row: int, column: int, value: Any, style: str) -> None:
        cell = self.worksheet.cell(row=row + 1, column=column + 1, value=value)
        resolved = self._style(style)
        cell.font = resolved["font"]
        cell.fill = resolved["fill"]
        cell.alignment = resolved["alignment"]
        if resolved["border"] is not None:
            cell.border = resolved["border"]
        if resolved["num_format"] != "General":
            cell.number_format = resolved["num_format"]

    def write(self, row: int, column: int, value: Any, style: str) -> None:
        self._apply(row, column, value, style)

    def merge(
        self,
        first_row: int,
        first_column: int,
        last_row: int,
        last_column: int,
        value: Any,
        style: str,
    ) -> None:
        if (first_row, first_column) != (last_row, last_column):
            self.worksheet.merge_cells(
                start_row=first_row + 1,
                start_column=first_column + 1,
                end_row=last_row + 1,
                end_column=last_column + 1,
            )
        self._apply(first_row, first_column, value, style)

    def set_width(self, column: int, width: float) -> None:
        self.worksheet.column_dimensions[get_column_letter(column + 1)].width = width

    def set_row_height(self, row: int, height: float) -> None:
        self.worksheet.row_dimensions[row + 1].height = height

    def freeze(self, row: int, column: int) -> None:
        self.worksheet.freeze_panes = self.worksheet.cell(row=row + 1, column=column + 1).coordinate

    def print_setup(self, *, landscape: bool, repeat_rows: tuple[int, int] | None) -> None:
        self.worksheet.page_setup.orientation = "landscape" if landscape else "portrait"
        self.worksheet.page_setup.paperSize = 9
        self.worksheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        self.worksheet.page_setup.fitToWidth = 1
        self.worksheet.page_setup.fitToHeight = 0
        if repeat_rows is not None:
            self.worksheet.print_title_rows = f"{repeat_rows[0] + 1}:{repeat_rows[1] + 1}"

    def autofilter(self, first_row: int, last_row: int, last_column: int) -> None:
        if last_row > first_row:
            self.worksheet.auto_filter.ref = (
                f"A{first_row + 1}:{get_column_letter(last_column + 1)}{last_row + 1}"
            )


# ==========================================================================
# Shared rendering
# ==========================================================================
CellStyleHook = Callable[[int, int, Any], str | None]
SheetFactory = Callable[[str], _SheetWriter]


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_section_row(row: Sequence[Any]) -> bool:
    return bool(row) and not _blank(row[0]) and all(_blank(cell) for cell in row[1:])


def _write_table(
    sheet: _SheetWriter, table: Table, *, cell_style: CellStyleHook | None = None
) -> None:
    """Render a canonical table: header row, optional section rows, data rows."""
    for row_index, row in enumerate(table):
        if not row or all(_blank(cell) for cell in row):
            continue
        if row_index == 0:
            default: str | None = "header"
        elif _is_section_row(row):
            default = "section"
        elif not _blank(row[0]) and str(row[0]) == "合计":
            default = "total"
        else:
            default = None
        for column, value in enumerate(row):
            if _blank(value):
                if default == "header":
                    sheet.write(row_index, column, None, default)
                continue
            if default is not None:
                style = default
            elif cell_style is not None and (hooked := cell_style(row_index, column, value)):
                style = hooked
            elif isinstance(value, float):
                style = "number"
            else:
                style = "plain"
            sheet.write(row_index, column, value, style)
    for column, width in enumerate(_column_widths(table)):
        sheet.set_width(column, float(min(48, max(8, width + 2))))


def _week_groups(dates: Sequence[dt.date]) -> list[tuple[int, int, str]]:
    """Consecutive dates grouped by ISO week: ``(first_offset, last_offset, label)``."""
    groups: list[list[Any]] = []
    for offset, date in enumerate(dates):
        key = date.isocalendar()[:2]
        if groups and groups[-1][0] == key:
            groups[-1][2] = offset
        else:
            groups.append([key, offset, offset])
    labels: list[tuple[int, int, str]] = []
    for key, start, end in groups:
        first, last = dates[start], dates[end]
        labels.append(
            (
                start,
                end,
                f"{key[0]}年第{key[1]}周  {first.month}/{first.day}-{last.month}/{last.day}",
            )
        )
    return labels


def _write_roster_grid(
    sheet: _SheetWriter,
    solution: Solution,
    instance: Instance,
    *,
    flagged: set[tuple[str, dt.date]],
) -> None:
    """The only human-readable view: person rows against date columns, week-banded."""
    view = csv_io.roster_view(solution, instance)
    leading = len(view.leading_headers)
    holidays = set(instance.holidays)
    dates_end = leading + len(view.dates)

    for index, name in enumerate(view.leading_headers):
        sheet.merge(0, index, 1, index, name, "header")
    for offset, name in enumerate(view.trailing_headers):
        sheet.merge(0, dates_end + offset, 1, dates_end + offset, name, "header")
    for start, end, label in _week_groups(view.dates):
        if start == end:
            sheet.write(0, leading + start, label, "week")
        else:
            sheet.merge(0, leading + start, 0, leading + end, label, "week")
    for offset, date in enumerate(view.dates):
        if date in holidays:
            style = "holiday"
        elif date.weekday() >= 5:
            style = "weekend"
        else:
            style = "date"
        sheet.write(1, leading + offset, f"{date.month}/{date.day}\n{weekday_cn(date)}", style)

    for row_index, row in enumerate(view.rows, start=2):
        person_id = str(row[1])
        for column in range(leading):
            sheet.write(row_index, column, row[column], "label" if column == 0 else "plain")
        for offset, cell in enumerate(row[leading:dates_end]):
            date = view.dates[offset]
            if _blank(cell):
                if date in holidays:
                    sheet.write(row_index, leading + offset, None, "holiday_blank")
                elif date.weekday() >= 5:
                    sheet.write(row_index, leading + offset, None, "weekend_blank")
                continue
            colour = shift_colour(str(cell))
            style = f"shift_flagged:{colour}" if (person_id, date) in flagged else f"shift:{colour}"
            sheet.write(row_index, leading + offset, csv_io.shift_label(instance, str(cell)), style)
        for offset, value in enumerate(row[dates_end:]):
            sheet.write(
                row_index,
                dates_end + offset,
                value,
                "number" if isinstance(value, float) else "plain",
            )

    sheet.set_width(0, 12)
    sheet.set_width(1, 12)
    sheet.set_width(2, 10)
    for offset in range(len(view.dates)):
        sheet.set_width(leading + offset, 6)
    sheet.set_row_height(0, 20)
    sheet.set_row_height(1, 30)
    sheet.freeze(2, leading)
    sheet.print_setup(landscape=True, repeat_rows=(0, 1))


def _flagged_cells(solution: Solution) -> set[tuple[str, dt.date]]:
    """Which assignment cells are suspect.

    ``verification_errors``/``conflicts`` are free-form strings, so this is
    best-effort: each message is scanned for a ``YYYY-MM-DD`` and a known person
    id, and a message that parses to nothing flags the whole roster.  An
    *unverified* solution always flags every cell -- an unverified roster is
    precisely what must not look authoritative.
    """
    assigned = {(assignment.person, assignment.date) for assignment in solution.assignments}
    people = {person for person, _ in assigned}
    flagged: set[tuple[str, dt.date]] = set()
    for message in (*solution.verification_errors, *solution.conflicts):
        dates: list[dt.date] = []
        for token in _ISO_DATE.findall(message):
            try:
                dates.append(dt.date.fromisoformat(token))
            except ValueError:
                continue
        named = {person for person in people if person in message}
        for person in named or people:
            for date in dates:
                if (person, date) in assigned:
                    flagged.add((person, date))
    if not solution.verified:
        flagged |= assigned
    return flagged


def _row_hook(marks: Mapping[int, Any]) -> CellStyleHook:
    """Red font for the long-sheet rows listed in ``marks``."""

    def hook(row: int, column: int, value: Any) -> str | None:
        del column, value
        return "flagged" if row in marks else None

    return hook


def _flagged_rows(table: Table, flagged: set[tuple[str, dt.date]]) -> dict[int, None]:
    """Row indices of the assignments table whose (person, date) is suspect."""
    marks: dict[int, None] = {}
    if not flagged:
        return marks
    for row_index, row in enumerate(table[1:], start=1):
        if len(row) < 4:
            continue
        try:
            date = dt.date.fromisoformat(str(row[3]))
        except ValueError:
            continue
        if (str(row[0]), date) in flagged:
            marks[row_index] = None
    return marks


def _coverage_hook() -> CellStyleHook:
    """Red on a shortfall, amber on an excess (columns 8 and 9 of the coverage table)."""

    def hook(row: int, column: int, value: Any) -> str | None:
        del row
        if column == 8 and isinstance(value, int) and value > 0:
            return "bad"
        if column == 9 and isinstance(value, int) and value > 0:
            return "warn"
        return None

    return hook


def _text_hook() -> CellStyleHook:
    """Wrap the value column of the key/value README so long caveats stay readable."""

    def hook(row: int, column: int, value: Any) -> str | None:
        del row, value
        return "text" if column == 1 else None

    return hook


def _write_readme_sheet(
    sheet: _SheetWriter,
    solution: Solution,
    instance: Instance,
    spec: Spec | None,
    legend: Mapping[str, str],
    layout: Layout,
) -> None:
    table = csv_io.readme_table(solution, instance, spec, legend=legend, layout=layout)
    table = [*table, [], ["【标注说明】", _ANNOTATION_NOTE]]
    _write_table(sheet, table, cell_style=_text_hook())
    sheet.set_width(0, 26)
    sheet.set_width(1, 96)


def _render_sheets(
    make_sheet: SheetFactory,
    solution: Solution,
    instance: Instance,
    spec: Spec | None,
    layout: Layout,
    flagged: set[tuple[str, dt.date]],
) -> None:
    """Write every sheet of the deliverable through one backend-neutral factory."""
    legend = {shift.id: shift_colour(shift.id) for shift in instance.shifts if shift.attendance}
    _write_readme_sheet(make_sheet(SHEET_README), solution, instance, spec, legend, layout)

    if layout != "long":
        _write_roster_grid(make_sheet(SHEET_ROSTER), solution, instance, flagged=flagged)

    assignments = csv_io.assignments_table(solution, instance)
    sheet = make_sheet(SHEET_ASSIGNMENTS)
    _write_table(sheet, assignments, cell_style=_row_hook(_flagged_rows(assignments, flagged)))
    sheet.freeze(1, 0)
    sheet.autofilter(0, len(assignments) - 1, max(len(row) for row in assignments) - 1)
    sheet.print_setup(landscape=False, repeat_rows=(0, 0))

    coverage = csv_io.coverage_table(solution, instance)
    sheet = make_sheet(SHEET_COVERAGE)
    _write_table(sheet, coverage, cell_style=_coverage_hook())
    sheet.freeze(1, 0)
    sheet.autofilter(0, len(coverage) - 1, max(len(row) for row in coverage) - 1)

    _write_table(make_sheet(SHEET_FAIRNESS), csv_io.fairness_table(solution, instance))
    _write_table(make_sheet(SHEET_VIOLATIONS), csv_io.violations_table(solution, instance))


# ==========================================================================
# Public API
# ==========================================================================
def export_workbook(
    solution: Solution,
    instance: Instance,
    *,
    spec: Spec | None = None,
    path: str | Path,
    layout: Layout = "monthly",
) -> Path:
    """Write the deliverable workbook with XlsxWriter.  Returns the path written.

    ``layout`` selects which views are rendered:

    * ``"monthly"`` (default) -- README, Roster (week-banded wide grid),
      Assignments, Coverage, Fairness, Violations: the file a 教务员 opens.
    * ``"long"`` -- the same minus the wide Roster, for a machine-only hand-off.
    * ``"both"`` -- identical to ``"monthly"``: inside a workbook the tidy sheet
      always accompanies the wide grid, so there is nothing extra to select.
    """
    key = _check_layout(layout)
    target = Path(path)
    if target.suffix.lower() != ".xlsx":
        target = target.with_suffix(".xlsx")
    target.parent.mkdir(parents=True, exist_ok=True)

    flagged = _flagged_cells(solution)
    workbook = xlsxwriter.Workbook(
        str(target), {"default_date_format": "yyyy-mm-dd", "strings_to_numbers": False}
    )
    try:
        formats: dict[str, Any] = {}

        def make_sheet(name: str) -> _SheetWriter:
            return _XlsxSheet(workbook.add_worksheet(name), workbook, formats)

        _render_sheets(make_sheet, solution, instance, spec, key, flagged)
    finally:
        workbook.close()
    return target


def export_into_template(
    template_path: str | Path,
    solution: Solution,
    instance: Instance,
    *,
    spec: Spec | None = None,
    path: str | Path | None = None,
    layout: Layout = "monthly",
    overwrite_template: bool = False,
) -> Path:
    """Fill a user's own workbook while preserving everything already in it.

    openpyxl is used with ``data_only=False`` so existing **formulas, styles and
    unrelated sheets survive**.  The six sheets this module owns are replaced;
    every other sheet is left untouched.  Because overwriting a colleague's
    template is destructive, the default writes to a *new* file
    ``<name>_filled.xlsx`` next to it -- pass ``overwrite_template=True`` (or an
    explicit ``path``) to write in place.  ``.xlsm`` templates are saved back with
    VBA preserved, but never renamed to ``.xlsx``.
    """
    key = _check_layout(layout)
    source = Path(template_path)
    if not source.exists():
        raise FileNotFoundError(f"template not found: {source}")
    if source.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValueError(f"template must be .xlsx or .xlsm, got {source.suffix!r}")
    if path is not None:
        target = Path(path)
    elif overwrite_template:
        target = source
    else:
        target = source.with_name(f"{source.stem}_filled{source.suffix}")
    if target.resolve() != source.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    workbook = load_workbook(target, data_only=False, keep_vba=source.suffix.lower() == ".xlsm")
    for name in OWNED_SHEETS:
        if name in workbook.sheetnames:
            del workbook[name]
    cache: dict[str, dict[str, Any]] = {}

    def make_sheet(name: str) -> _SheetWriter:
        return _OpenpyxlSheet(workbook.create_sheet(title=name), cache)

    flagged = _flagged_cells(solution)
    _render_sheets(make_sheet, solution, instance, spec, key, flagged)
    workbook.save(target)
    return target


def export_csv(
    solution: Solution,
    instance: Instance,
    *,
    out_dir: str | Path,
    layout: Layout = "both",
    spec: Spec | None = None,
) -> list[Path]:
    """Write the same views as one CSV file each (UTF-8 **with BOM** for Excel).

    Because a CSV cannot hold several sheets, ``layout`` selects *files* here:
    ``"monthly"`` writes ``roster``, ``"long"`` writes ``assignments`` +
    ``coverage`` + ``fairness`` + ``violations``, ``"both"`` (default) writes all
    of them.  ``README.csv`` (spec/solver facts, assumptions, colour legend) is
    always written.  Returns the paths in write order.
    """
    key = _check_layout(layout)
    legend = {shift.id: shift_colour(shift.id) for shift in instance.shifts if shift.attendance}
    tables: dict[str, Table] = {}
    readme = csv_io.readme_table(
        solution, instance, spec, legend=legend if key != "long" else None, layout=key
    )
    tables["README"] = [*readme, [], ["【标注说明】", _ANNOTATION_NOTE]]
    if key in ("monthly", "both"):
        tables["roster"] = csv_io.roster_table(solution, instance)
    if key in ("long", "both"):
        tables["assignments"] = csv_io.assignments_table(solution, instance)
        tables["coverage"] = csv_io.coverage_table(solution, instance)
        tables["fairness"] = csv_io.fairness_table(solution, instance)
        tables["violations"] = csv_io.violations_table(solution, instance)
    return csv_io.write_csv_tables(out_dir, tables)
