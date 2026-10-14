"""Data contracts. Nothing in this package may import from ``model`` or ``solve``."""

from .constraints import ConstraintSet, HardConstraint, SoftConstraint
from .instance import (
    DemandRule,
    Group,
    Horizon,
    Instance,
    Person,
    Preference,
    Shift,
    UnavailabilityRule,
    resolve_groups,
    summarise,
)
from .solution import (
    Assignment,
    CoverageRow,
    FairnessMetric,
    ObjectiveTerm,
    PersonStats,
    Solution,
    SolveStatus,
)
from .spec import CURRENT_SCHEMA_VERSION, SolverParams, Spec, validate_text

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "Assignment",
    "ConstraintSet",
    "CoverageRow",
    "DemandRule",
    "FairnessMetric",
    "Group",
    "HardConstraint",
    "Horizon",
    "Instance",
    "ObjectiveTerm",
    "Person",
    "PersonStats",
    "Preference",
    "Shift",
    "SoftConstraint",
    "Solution",
    "SolveStatus",
    "SolverParams",
    "Spec",
    "UnavailabilityRule",
    "resolve_groups",
    "summarise",
    "validate_text",
]
