# (This module deliberately emits Chinese text: full-width punctuation here is
#  user-facing output, never a confusable-character typo.)
"""Excel/CSV I/O: user workbooks -> schema objects, and solutions -> workbooks.

Import is deliberately two-step: a :class:`~scheduling_or.io.excel_import.WorkbookMap`
declares which user column feeds which schema field, so a messy 教务员 workbook is
never reshaped by hand and no column name is ever guessed.

Export always produces the two views from ``docs/plans`` §11.1 -- the wide monthly
grid (``Roster``) and the tidy long sheet (``Assignments``) -- because a roster
nobody can read is a roster nobody checks.

Public entry points:

* :func:`import_instance` -- workbook + mapping -> instance facts + problems
* :func:`export_workbook` -- new XLSX deliverable (XlsxWriter)
* :func:`export_into_template` -- fill a user's own workbook (openpyxl, preserves it)
* :func:`export_csv` -- the same views as UTF-8-with-BOM CSV files
* :func:`read_csv_rows` -- the shared CSV reader the importers use
"""

from .csv_io import read_csv_rows, read_csv_table, write_csv_rows
from .excel_export import export_csv, export_into_template, export_workbook, shift_colour
from .excel_import import (
    CoercionError,
    ColumnMap,
    ImportResult,
    InstanceParts,
    Loaded,
    SheetMap,
    WorkbookMap,
    import_instance,
    load_demand,
    load_holidays,
    load_preferences,
    load_shifts,
    load_staff,
    load_unavailability,
    parse_bool,
    parse_date,
    parse_float,
    parse_int,
    parse_list,
    parse_time,
    parse_weekday,
    read_sheets,
)

__all__ = [
    "CoercionError",
    "ColumnMap",
    "ImportResult",
    "InstanceParts",
    "Loaded",
    "SheetMap",
    "WorkbookMap",
    "export_csv",
    "export_into_template",
    "export_workbook",
    "import_instance",
    "load_demand",
    "load_holidays",
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
    "read_csv_rows",
    "read_csv_table",
    "read_sheets",
    "shift_colour",
    "write_csv_rows",
]
