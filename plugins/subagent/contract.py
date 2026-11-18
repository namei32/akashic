"""Read-only liveness of a background child belonging to the given parent session."""
from collections.abc import Callable

from agent.plugin_composition import ServiceKey

SUBAGENT_WAITING = ServiceKey[Callable[[str, str], bool]]("subagent.waiting.v1")
