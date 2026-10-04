"""GemBot state package: JSON state files on the ``bot-state`` branch (see ``store.py``)."""

from gembot.state.store import (
    PRUNE_KEYS,
    SCHEMA_VERSION,
    STATE_FILES,
    GitStateRepo,
    StateStore,
    ensure_state_branch,
    estimate_size,
    prune,
    serialize_state,
)

__all__ = [
    "PRUNE_KEYS",
    "SCHEMA_VERSION",
    "STATE_FILES",
    "GitStateRepo",
    "StateStore",
    "ensure_state_branch",
    "estimate_size",
    "prune",
    "serialize_state",
]
