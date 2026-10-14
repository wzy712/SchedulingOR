"""Solving: pre-check, pipeline orchestration, infeasibility diagnosis."""

from .diagnose import Diagnosis, diagnose, suggestions_for
from .pipeline import SolveOutcome, SolveRequest, solve
from .precheck import Precheck, Problem, run_precheck

__all__ = [
    "Diagnosis",
    "Precheck",
    "Problem",
    "SolveOutcome",
    "SolveRequest",
    "diagnose",
    "run_precheck",
    "solve",
    "suggestions_for",
]
