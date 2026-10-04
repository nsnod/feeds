"""GemBot scoring: features -> Gem Score -> reasons -> alarm / roundup decisions.

See ``docs/SCORING.md`` for the plain-English explanation of every number.
"""

from __future__ import annotations

from gembot.scoring.decide import PostPlan, commit_plan, is_roundup_due, plan_posts, qualifies_for_alarm
from gembot.scoring.explain import build_reasons
from gembot.scoring.features import ScoringContext, baseline_eph, compute_features
from gembot.scoring.score import blocklist_reason, prefilter, score_game
from gembot.scoring.weights import current_weights, describe_change, normalize, update_weights

__all__ = [
    "PostPlan",
    "ScoringContext",
    "baseline_eph",
    "blocklist_reason",
    "build_reasons",
    "commit_plan",
    "compute_features",
    "current_weights",
    "describe_change",
    "is_roundup_due",
    "normalize",
    "plan_posts",
    "prefilter",
    "qualifies_for_alarm",
    "score_game",
    "update_weights",
]
