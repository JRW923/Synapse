"""CLI planner selection (--mode) maps each mode to the right Planner.

Locks in that ``--mode swarm`` (TODO C) reaches SwarmPlanner through the same
CLI wiring used by `run` / `chat`.
"""

from synapse.config.schema import SynapseConfig
from synapse.adapters.cli import _create_planner
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.plan_execute import PlanExecutePlanner
from synapse.modules.planning.hierarchical import HierarchicalPlanner
from synapse.modules.planning.swarm import SwarmPlanner


def _select(mode: str):
    cfg = SynapseConfig()
    cfg.planning.mode = mode
    return _create_planner(cfg, auth=object())


def test_mode_selection():
    assert isinstance(_select("react"), ReActPlanner)
    assert isinstance(_select("plan_execute"), PlanExecutePlanner)
    assert isinstance(_select("hierarchical"), HierarchicalPlanner)
    assert isinstance(_select("swarm"), SwarmPlanner)
