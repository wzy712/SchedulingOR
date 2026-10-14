"""Assemble a CP-SAT model from a spec.

The builder knows *nothing* about individual constraint semantics: it creates the
variables, instantiates the registered modules for each spec constraint, and hands
back the model plus the objective ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from ..schema.spec import Spec
from .modules import BuildContext, ConstraintModule, Ledger, instantiate
from .modules.hard import apply_instance_invariants
from .vars import DecisionVars, create_vars


@dataclass
class BuiltModel:
    spec: Spec
    model: cp_model.CpModel
    vars: DecisionVars
    ledger: Ledger
    ctx: BuildContext
    hard_modules: list[ConstraintModule] = field(default_factory=list)
    soft_modules: list[ConstraintModule] = field(default_factory=list)
    #: soft modules that were deliberately left out (e.g. for a feasibility probe)
    deferred_soft: list[ConstraintModule] = field(default_factory=list)

    @property
    def notes(self) -> list[str]:
        return list(self.ctx.notes)


def build(spec: Spec, *, include_soft: bool = True) -> BuiltModel:
    """Create variables and add every enabled constraint.

    ``include_soft=False`` produces the pure feasibility model used by the
    diagnostic ladder -- and, per the CP-SAT docs, the only configuration in which
    ``sufficient_assumptions_for_infeasibility`` actually minimises the core.
    """
    model = cp_model.CpModel()
    instance = spec.instance
    groups = instance.resolved_groups()
    vars_ = create_vars(model, instance)

    ctx = BuildContext(
        instance=instance, spec=spec, model=model, vars=vars_, groups=groups, ledger=Ledger()
    )

    hard_modules: list[ConstraintModule] = []
    soft_modules: list[ConstraintModule] = []
    deferred: list[ConstraintModule] = []

    # instance-level facts come first: they are not optional policy
    apply_instance_invariants(ctx)

    for cfg in spec.constraints.hard:
        if not getattr(cfg, "enabled", True):
            continue
        module = instantiate(cfg)
        module.build(ctx)
        hard_modules.append(module)

    for cfg in spec.constraints.soft:
        if not getattr(cfg, "enabled", True):
            continue
        module = instantiate(cfg)
        if include_soft:
            module.build(ctx)
            soft_modules.append(module)
        else:
            deferred.append(module)

    return BuiltModel(
        spec=spec,
        model=model,
        vars=vars_,
        ledger=ctx.ledger,
        ctx=ctx,
        hard_modules=hard_modules,
        soft_modules=soft_modules,
        deferred_soft=deferred,
    )


def objective_expression(built: BuiltModel, only: set[str] | None = None) -> object:
    return built.ledger.expression(only=only)
