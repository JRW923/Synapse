"""
Synapse — Connecting ideas into code.
"""

__version__ = "0.1.0"

from synapse.adapters.library import Synapse  # noqa: E402
from synapse.core.agent import Agent         # noqa: E402

__all__ = ["Synapse", "Agent", "__version__"]
