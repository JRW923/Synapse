"""Security red-team / adversarial testing framework (TODO F).

A curated attack library plus a deterministic, reproducible runner that drives
the agent with a scripted LLM and scores whether the existing defenses
(ActionAuthorizer / ProcessSandbox / InjectionGuard) neutralize each attack.

No real LLM or network access is required — every attack carries the exact
tool calls the (hypothetical) attacker would issue, replayed by ``AttackLLM``.
"""

from synapse.eval.redteam.attacks import (
    AttackCategory,
    AttackCase,
    AttackStep,
    DefenseOutcome,
    seed_attacks,
)
from synapse.eval.redteam.runner import (
    AttackLLM,
    AttackResult,
    RedTeamReport,
    RedTeamRunner,
)

__all__ = [
    "AttackCategory",
    "AttackCase",
    "AttackStep",
    "DefenseOutcome",
    "seed_attacks",
    "AttackLLM",
    "AttackResult",
    "RedTeamReport",
    "RedTeamRunner",
]
