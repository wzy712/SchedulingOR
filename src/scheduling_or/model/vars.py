"""Decision variables for the ``(person, date, shift)`` assignment model.

Index convention (fixed for the whole project):

    x[p, d, s]  in {0,1}   s in real shifts + the rest code
    works[p, d] in {0,1}   == OR over the real shifts, i.e. "did this person work at all"

``works`` is materialised once here rather than recomputed in every module: the
sequence constraints (max consecutive days, weekly rest, consecutive preference)
all need it, and duplicating it would create redundant variables and inconsistent
enforcement.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from ..schema.instance import Instance


@dataclass
class DecisionVars:
    dates: list[dt.date]
    people: list[str]
    shifts: list[str]
    rest: str
    x: dict[tuple[str, dt.date, str], cp_model.BoolVar] = field(default_factory=dict)
    works: dict[tuple[str, dt.date], cp_model.IntVar] = field(default_factory=dict)

    def on(self, person: str, date: dt.date, shift: str) -> cp_model.BoolVar:
        return self.x[person, date, shift]

    def day(self, person: str, date: dt.date) -> list[cp_model.BoolVar]:
        """All *real* shift variables for a person-day (rest excluded)."""
        return [self.x[person, date, s] for s in self.shifts]

    def worked(self, person: str, date: dt.date) -> cp_model.IntVar:
        return self.works[person, date]

    def iter_person_days(self) -> Iterator[tuple[str, dt.date]]:
        for p in self.people:
            for d in self.dates:
                yield p, d

    @property
    def n_booleans(self) -> int:
        return len(self.x) + len(self.works)


def create_vars(model: cp_model.CpModel, instance: Instance) -> DecisionVars:
    dates = instance.dates()
    people = instance.person_ids
    shifts = instance.shift_ids
    rest = instance.rest_code

    v = DecisionVars(dates=dates, people=people, shifts=shifts, rest=rest)

    for p in people:
        for d in dates:
            for s in [*shifts, rest]:
                v.x[p, d, s] = model.new_bool_var(f"x_{p}_{d.isoformat()}_{s}")

    # works[p, d] == max(x[p, d, s] for s in shifts)
    # Exact both ways: >= every shift (so any shift implies working) and <= their
    # sum (so it cannot be 1 while all shifts are 0).
    for p, d in v.iter_person_days():
        w = model.new_int_var(0, 1, f"works_{p}_{d.isoformat()}")
        real = v.day(p, d)
        if real:
            model.add_max_equality(w, list(real))
        else:  # degenerate: no real shifts at all
            model.add(w == 0)
        v.works[p, d] = w

    return v


def prorated_rest(min_per_week: int, chunk_len: int) -> int:
    """Rest days required in a chunk of ``chunk_len`` days.

    A naive ``rest >= min_per_week`` applied to a trailing 2-day chunk would demand
    2 rest days out of 2 days -- i.e. nobody may work the last two days of a month
    that does not end on a week boundary. Measured: that silently cost 2 uncovered
    shifts in the duty-roster seed and looked like a solver failure.

    Prorating by ``ceil(min * len / 7)`` keeps the intent (a *rate* of rest) while
    staying satisfiable at the boundaries. Full 7-day chunks are unaffected.
    """
    if chunk_len >= 7:
        return min_per_week
    scaled = -(-min_per_week * chunk_len // 7)  # ceil division, integer
    return max(0, min(scaled, chunk_len))


def period_chunks(dates: list[dt.date], period: str) -> list[list[dt.date]]:
    """Split the horizon into consecutive chunks.

    ``week`` chunks start on the horizon's first date (so a partial month does not
    invent calendar weeks that are almost entirely outside the horizon) and are
    7 days long; the final chunk may be shorter. ``horizon`` is a single chunk.
    """
    if period == "horizon":
        return [list(dates)]
    return [dates[i : i + 7] for i in range(0, len(dates), 7)]


def history_works(
    instance: Instance, person: str, date: dt.date, lookback_days: int
) -> list[int]:
    """Constant 0/1 values for the ``lookback_days`` days before the horizon.

    Returns oldest-first so it can be prepended to the ``works`` series. Without
    this, a streak that started last month would reset at the horizon boundary and
    the max-consecutive rule would silently under-count.
    """
    if lookback_days <= 0:
        return []
    worked: set[dt.date] = set()
    for entry in next(p for p in instance.people if p.id == person).history:
        worked.add(entry.date)
    start = date - dt.timedelta(days=lookback_days)
    return [1 if (start + dt.timedelta(days=i)) in worked else 0 for i in range(lookback_days)]
