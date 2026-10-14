"""Top-level spec: the single contract between the agent and the kernel.

A spec is *the* artefact the interview produces. It bundles the facts
(``instance``), the policy (``constraints``), the knobs (``solver``) and the
agent's own uncertainty (``assumptions`` / ``unresolved``) into one reviewable,
version-controlled YAML file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .constraints import ConstraintSet
from .instance import Instance

_STRICT = ConfigDict(extra="forbid")

CURRENT_SCHEMA_VERSION = 1


class SolverParams(BaseModel):
    model_config = _STRICT

    max_time_seconds: float = Field(default=60.0, gt=0, le=3600)
    num_workers: int = Field(default=8, ge=1, le=64)
    random_seed: int = Field(default=0, ge=0)
    strategy: Literal["lexicographic", "weighted_sum"] = "lexicographic"
    relative_gap_limit: float = Field(default=0.0, ge=0, le=1)
    log_search_progress: bool = False
    #: in lexicographic mode, pin phase-1 feasibility before optimising soft goals
    freeze_feasible: bool = True


class Spec(BaseModel):
    model_config = _STRICT

    schema_version: int = CURRENT_SCHEMA_VERSION
    name: str = ""
    instance: Instance
    constraints: ConstraintSet = Field(default_factory=ConstraintSet)
    solver: SolverParams = Field(default_factory=SolverParams)
    #: inferences the agent made; every one of these must be confirmed by the user
    assumptions: list[str] = Field(default_factory=list)
    #: questions the agent could not resolve; deliberately left open
    unresolved: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _version(self) -> Spec:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {self.schema_version}; "
                f"this build understands {CURRENT_SCHEMA_VERSION} (run `sched migrate`)"
            )
        return self

    @model_validator(mode="after")
    def _assign_constraint_ids(self) -> Spec:
        """Give every constraint a stable, unique id.

        Diagnostics and the cost report key off these ids, so an empty id must
        never survive into the model.
        """
        used: set[str] = set()
        counters: dict[str, int] = {}
        for group in (self.constraints.hard, self.constraints.soft):
            for c in group:
                if c.id:
                    base = c.id
                else:
                    counters[c.type] = counters.get(c.type, 0) + 1
                    base = f"{c.type}#{counters[c.type]}"
                if base in used:
                    counters[base] = counters.get(base, 0) + 1
                    base = f"{base}~{counters[base]}"
                used.add(base)
                c.id = base
        return self

    # -- io ---------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> Spec:
        text = Path(path).read_text(encoding="utf-8")
        if str(path).endswith(".json"):
            return cls.model_validate_json(text)
        return cls.model_validate(yaml.safe_load(text))

    def dump(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", by_alias=True, exclude_none=False)
        if p.suffix == ".json":
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            p.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100),
                encoding="utf-8",
            )

    @classmethod
    def loads(cls, text: str, fmt: Literal["yaml", "json"] = "yaml") -> Spec:
        if fmt == "json":
            return cls.model_validate_json(text)
        return cls.model_validate(yaml.safe_load(text))

    def dumps(self, fmt: Literal["yaml", "json"] = "yaml") -> str:
        data = self.model_dump(mode="json", by_alias=True, exclude_none=False)
        if fmt == "json":
            return json.dumps(data, ensure_ascii=False, indent=2)
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)

    def validate_ids_referenced(self) -> list[str]:
        """Cross-references JSON Schema cannot express. Returns human-readable problems."""
        problems: list[str] = []
        inst = self.instance
        shift_ids = {s.id for s in inst.shifts}
        person_ids = set(inst.person_ids)
        try:
            group_ids = set(inst.resolved_groups())
        except Exception as exc:
            return [f"groups: {exc}"]

        for s in inst.shifts:
            if s.attendance and s.resolved_hours() <= 0:
                problems.append(f"shift {s.id!r} has non-positive duration")
        for rule in inst.demand:
            if rule.shift not in shift_ids:
                problems.append(f"demand references unknown shift {rule.shift!r}")
            if rule.shift == inst.rest_code:
                problems.append(f"demand must not target the rest code {inst.rest_code!r}")
        for rule in inst.unavailability:
            if rule.person not in person_ids:
                problems.append(f"unavailability references unknown person {rule.person!r}")
            for s in rule.shifts:
                if s not in shift_ids:
                    problems.append(f"unavailability references unknown shift {s!r}")
        for pref in inst.preferences:
            if pref.person not in person_ids:
                problems.append(f"preference references unknown person {pref.person!r}")
            if pref.shift not in shift_ids:
                problems.append(f"preference references unknown shift {pref.shift!r}")

        def check_group(gid: str | None, where: str) -> None:
            if gid is not None and gid not in group_ids:
                problems.append(f"{where} references unknown group {gid!r}")

        for c in self.constraints.hard:
            t = c.type
            if t in ("requirement",):
                if c.shift not in shift_ids:
                    problems.append(f"{c.id}: unknown shift {c.shift!r}")
                check_group(c.group, c.id)
            elif t in ("max_shifts_per_period", "hours_bounds"):
                check_group(getattr(c, "group", None), c.id)
            elif t in ("forbid_transition", "penalized_transition"):
                for attr in ("from_shift", "to_shift"):
                    if getattr(c, attr) not in shift_ids:
                        problems.append(f"{c.id}: unknown shift {getattr(c, attr)!r}")
            elif t == "fixed_assignment":
                if c.person not in person_ids:
                    problems.append(f"{c.id}: unknown person {c.person!r}")
            elif t == "no_pair":
                for pid in c.people:
                    if pid not in person_ids:
                        problems.append(f"{c.id}: unknown person {pid!r}")
            elif t == "pairing":
                check_group(c.junior_group, c.id)
                check_group(c.senior_group, c.id)
        for c in self.constraints.soft:
            if c.type == "fairness":
                metric = c.metric
                if metric.startswith("shift:") and metric.split(":", 1)[1] not in shift_ids:
                    problems.append(f"{c.id}: fairness references unknown shift {metric!r}")
                check_group(getattr(c, "group", None), c.id)
            elif c.type == "target_shifts_deviation":
                check_group(getattr(c, "group", None), c.id)
        return problems

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema for the model, so an agent can see what it is allowed to write."""
        return self.model_json_schema()


def validate_text(text: str, fmt: Literal["yaml", "json"] = "yaml") -> dict[str, Any]:
    """Validate a candidate spec. Never raises: returns structured errors instead.

    This shape exists so a model can self-correct: it needs ``loc``/``type``/``msg``,
    not a traceback.
    """
    try:
        spec = Spec.loads(text, fmt)
    except ValidationError as exc:
        return {
            "ok": False,
            "stage": "schema",
            "errors": [
                {"loc": list(e["loc"]), "type": e["type"], "msg": e["msg"]}
                for e in exc.errors(include_url=False, include_input=False)
            ],
        }
    except yaml.YAMLError as exc:
        return {"ok": False, "stage": "parse", "errors": [{"loc": [], "type": "yaml", "msg": str(exc)}]}
    except Exception as exc:
        return {"ok": False, "stage": "parse", "errors": [{"loc": [], "type": type(exc).__name__, "msg": str(exc)}]}

    problems = spec.validate_ids_referenced()
    if problems:
        return {
            "ok": False,
            "stage": "semantics",
            "errors": [{"loc": [], "type": "reference", "msg": p} for p in problems],
        }
    return {
        "ok": True,
        "stage": "done",
        "errors": [],
        "summary": {
            "name": spec.name,
            "people": len(spec.instance.people),
            "shifts": len(spec.instance.shift_ids),
            "dates": len(spec.instance.dates()),
            "hard": len(spec.constraints.hard),
            "soft": len(spec.constraints.soft),
            "assumptions": len(spec.assumptions),
            "unresolved": len(spec.unresolved),
        },
    }
