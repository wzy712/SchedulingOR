"""Layered configuration.

Merge order (later wins):

    config/base.yaml               通用默认
    config/institutions/<name>.yaml 机构/场景特有
    config/runs/<run>.yaml          本次运行
    --set a.b=c                     命令行临时覆盖

Deep merge is done with OmegaConf (it keeps provenance, which matters when a user
asks "why is this weight 50?"), then the merged tree is validated and frozen by
pydantic. Hydra was rejected deliberately: it hijacks the entry point, changes the
working directory and rewrites ``sys.argv`` -- hostile when the process is spawned
as an MCP stdio server.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .schema.spec import Spec

_STRICT = ConfigDict(extra="forbid")


class SolverDefaults(BaseModel):
    model_config = _STRICT

    max_time_seconds: float = 60.0
    num_workers: int = 8
    random_seed: int = 0
    strategy: Literal["lexicographic", "weighted_sum"] = "lexicographic"
    relative_gap_limit: float = 0.0
    log_search_progress: bool = False


class WeightPreset(BaseModel):
    """Named knob values the interview can apply wholesale.

    ``weight: 0`` disables a soft constraint rather than deleting it, so an A/B
    comparison stays a one-line config diff.
    """

    model_config = _STRICT

    understaff: int = 1000
    overstaff: int = 20
    skill_shortfall: int = 800
    shift_spread: int = 50
    weekend_spread: int = 50
    holiday_spread: int = 50
    preference: int = 30
    consecutive_preference: int = 10
    transition: int = 10

    def as_dict(self) -> dict[str, int]:
        return self.model_dump()

    def for_constraint(self, type_name: str, metric: str | None = None) -> int | None:
        """The configured weight for a soft-constraint type, or None if untouched.

        Granularity is per *type* (plus the fairness metric), not per constraint id:
        ids are auto-derived and would be unstable to write by hand in config.
        """
        if type_name == "coverage_shortfall":
            return self.understaff
        if type_name == "coverage_excess":
            return self.overstaff
        if type_name == "skill_shortfall":
            return self.skill_shortfall
        if type_name == "preference":
            return self.preference
        if type_name == "consecutive_work_preference":
            return self.consecutive_preference
        if type_name == "penalized_transition":
            return self.transition
        if type_name == "fairness":
            if metric == "weekend_shifts":
                return self.weekend_spread
            if metric == "holiday_shifts":
                return self.holiday_spread
            return self.shift_spread
        return None


class ExportConfig(BaseModel):
    model_config = _STRICT

    formats: list[Literal["xlsx", "csv", "md", "json"]] = Field(default_factory=lambda: ["xlsx", "md"])
    layout: Literal["monthly", "flat"] = "monthly"
    title: str = ""
    #: deterministic shift -> hex colour, so the same shift looks the same everywhere
    shift_colors: dict[str, str] = Field(default_factory=dict)
    sheets: list[str] = Field(
        default_factory=lambda: [
            "README",
            "Roster",
            "Assignments",
            "Coverage",
            "Fairness",
            "Violations",
        ]
    )
    freeze_header_row: int = 2
    highlight_weekends: bool = True


class ColumnMap(BaseModel):
    model_config = _STRICT

    field: str
    column: str | int


class SheetMap(BaseModel):
    model_config = _STRICT

    sheet: str
    header_row: int = Field(default=1, ge=1)
    skip_rows: int = Field(default=0, ge=0)
    columns: list[ColumnMap] = Field(default_factory=list)


class ImportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    staff: SheetMap | None = None
    preferences: SheetMap | None = None
    unavailability: SheetMap | None = None
    demand: SheetMap | None = None
    shifts: SheetMap | None = None
    #: default attribute values applied when a column is missing
    defaults: dict[str, Any] = Field(default_factory=dict)


ConfigPolicy = Literal["override", "fill_missing", "ignore"]


class AppConfig(BaseModel):
    model_config = _STRICT

    schema_version: int = 1
    institution: str = ""
    #: How the config's ``weights`` and ``solver`` sections interact with the spec.
    #:
    #: ``fill_missing`` -- **default**: the spec states the priorities the interview
    #:                     produced; config only supplies what the spec left unstated.
    #: ``override``     -- config wins outright. Institution/run files set this, and
    #:                     ``--set`` implies it, so tuning always takes effect.
    #: ``ignore``       -- the spec is used verbatim (reproducing an old run).
    #:
    #: The default was ``override`` and that was wrong: the MCP ``solve`` tool composes
    #: the config without naming an institution, so ``base.yaml``'s generic defaults
    #: silently flattened a spec's deliberately graded fairness weights
    #: (200/120/80 -> 50/50/50). A default that clobbers authored intent is worse than
    #: one that occasionally needs an explicit opt-in.
    config_policy: ConfigPolicy = "fill_missing"
    solver: SolverDefaults = Field(default_factory=SolverDefaults)
    weights: WeightPreset = Field(default_factory=WeightPreset)
    export: ExportConfig = Field(default_factory=ExportConfig)
    excel_import: ImportConfig = Field(default_factory=ImportConfig, alias="import")
    #: free-form notes surfaced in the exported README sheet
    notes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def merged_notes(self) -> list[str]:
        return list(self.notes)


def config_root(start: Path | None = None) -> Path:
    """Locate the config directory: $SCHEDULINGOR_CONFIG, else ./config walking up."""
    env = os.environ.get("SCHEDULINGOR_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "base.yaml").is_file():
            return candidate / "config"
    return here / "config"


def _load_yaml(path: Path) -> DictConfig:
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return OmegaConf.create(data)


def compose(
    *,
    base: Path | None = None,
    institution: str | None = None,
    run: str | None = None,
    overrides: list[str] | None = None,
    root: Path | None = None,
) -> AppConfig:
    """Compose and validate the effective configuration."""
    where = root or config_root()
    layers: list[DictConfig] = []

    base_path = base or (where / "base.yaml")
    if base_path.is_file():
        layers.append(_load_yaml(base_path))
    else:
        layers.append(OmegaConf.create({}))

    if institution:
        inst_path = where / "institutions" / f"{institution}.yaml"
        layers.append(_load_yaml(inst_path))
    if run:
        run_path = where / "runs" / f"{run}.yaml"
        layers.append(_load_yaml(run_path))

    merged = OmegaConf.merge(*layers) if layers else OmegaConf.create({})
    if overrides:
        merged = OmegaConf.merge(merged, OmegaConf.from_dotlist(overrides))
        # Passing --set is an explicit instruction: honour it even though the default
        # policy is fill_missing. Without this, `-s weights.x=0` would silently no-op
        # whenever the spec already stated that weight -- the exact bug class this
        # whole mechanism exists to prevent.
        if not any(o.split("=", 1)[0].strip() == "config_policy" for o in overrides):
            merged = OmegaConf.merge(merged, {"config_policy": "override"})

    raw = OmegaConf.to_container(merged, resolve=True)
    if not isinstance(raw, dict):
        raise ValueError(f"configuration must be a mapping, got {type(raw).__name__}")
    if institution and not raw.get("institution"):
        raw["institution"] = institution
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(
            "invalid configuration:\n"
            + "\n".join(
                f"  - {'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors(include_url=False, include_input=False)
            )
        ) from exc


def apply_solver_params(spec: Spec, app: AppConfig) -> list[str]:
    """Push the config's solver section into the spec before solving.

    Same rationale as :func:`apply_weights`: this section was previously read from
    the file and then never used, so ``--set solver.max_time_seconds=300`` silently
    did nothing.
    """
    if app.config_policy == "ignore":
        return []
    changes: list[str] = []
    for field_name, value in app.solver.model_dump().items():
        if app.config_policy == "fill_missing" and field_name in spec.solver.model_fields_set:
            continue
        old = getattr(spec.solver, field_name)
        if old != value:
            changes.append(f"solver.{field_name}: {old} -> {value}")
            setattr(spec.solver, field_name, value)
    return changes


def apply_weights(spec: Spec, app: AppConfig) -> list[str]:
    """Push config weights into the spec's soft constraints before solving.

    Returns a human-readable log of every change, which is written into the run
    directory and added to the solver notes. Without that log a weight change is
    invisible -- and an invisible no-op is precisely how the earlier bug (config
    weights were computed but never applied) survived a full test suite.
    """
    if app.config_policy == "ignore":
        return []
    changes: list[str] = []
    for c in spec.constraints.soft:
        type_name = getattr(c, "type", "")
        new = app.weights.for_constraint(type_name, getattr(c, "metric", None))
        if new is None:
            continue
        if app.config_policy == "fill_missing" and "weight" in c.model_fields_set:
            continue
        old = int(getattr(c, "weight", 0))
        if old != int(new):
            changes.append(f"{c.id} ({type_name}): {old} -> {new}")
            c.weight = int(new)  # type: ignore[assignment]
    return changes


def apply_weights_to_spec(spec_dict: dict[str, Any], weights: WeightPreset) -> dict[str, Any]:
    """Rewrite a spec's soft-constraint weights from the config preset.

    Kept as a pure function on the *serialised* spec so the same helper works for
    the CLI, the MCP server and tests without importing the solver.
    """
    mapping = {
        "coverage_shortfall": weights.understaff,
        "coverage_excess": weights.overstaff,
        "preference": weights.preference,
        "penalized_transition": weights.transition,
        "consecutive_work_preference": weights.consecutive_preference,
    }
    for item in spec_dict.get("constraints", {}).get("soft", []):
        t = item.get("type")
        if t in mapping:
            item["weight"] = mapping[t]
        elif t == "fairness":
            metric = item.get("metric", "total_shifts")
            item["weight"] = (
                weights.weekend_spread if metric == "weekend_shifts" else weights.shift_spread
            )
    return spec_dict


def describe(app: AppConfig) -> str:
    return (
        f"institution={app.institution or '(base)'} "
        f"strategy={app.solver.strategy} time={app.solver.max_time_seconds}s "
        f"workers={app.solver.num_workers} formats={app.export.formats}"
    )
