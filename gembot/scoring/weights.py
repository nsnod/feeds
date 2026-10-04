"""Feature weights and the 👍/👎 learning rule.

Update rule (BUILD_SPEC 4.5), applied once per labelled post::

    w_i <- clip(w_i * (1 + lr * label * (f_i - mean_f)), weight_min, weight_max)
    then renormalise so the weights sum to 1

Plain renormalising can push a weight back outside ``[weight_min, weight_max]`` (for
example 0.5 / 0.98 = 0.51). ``bounded_normalize`` therefore finds one common scale factor
``c`` such that ``sum(clip(c * w_i)) == 1``: the result keeps the proportions of every
weight that is not at a bound, sums to 1 and respects the bounds whenever that is possible
(``n * weight_min <= 1 <= n * weight_max``). This is what "clip and renormalise until it
settles" converges to, computed exactly.
"""

from __future__ import annotations

import math

from gembot.config import FeedbackSettings
from gembot.models import FEATURE_NAMES, Features, LabelExample, WeightsState

NOTE_MIN_CHANGE = 0.005  # describe_change stays silent below this

STRONG_PHRASES = {
    "velocity": "fast-rising games",
    "underdog": "games from small creators with big reactions",
    "cross": "games with buzz on several platforms",
    "fit": "games with a clear co-op/friendslop fit",
    "hype": "games with strong comment hype",
    "meme": "games with Roblox-clone jokes",
    "fresh": "brand-new finds",
}
WEAK_PHRASES = {
    "velocity": "slow-burning games",
    "underdog": "games from bigger creators",
    "cross": "games seen on only one platform",
    "fit": "games with a weak co-op fit",
    "hype": "games with little comment hype",
    "meme": "games without Roblox-clone jokes",
    "fresh": "games we had known about for a while",
}


def _finite_nonneg(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number < 0:
        return None
    return number


def normalize(weights: dict[str, float]) -> dict[str, float]:
    """All seven feature weights, summing to 1 (unknown names dropped, bad values -> 0;
    all zero -> equal weights)."""
    clean = {name: _finite_nonneg(weights.get(name, 0.0)) or 0.0 for name in FEATURE_NAMES}
    total = sum(clean.values())
    if total <= 0:
        return {name: 1.0 / len(FEATURE_NAMES) for name in FEATURE_NAMES}
    return {name: value / total for name, value in clean.items()}


def bounded_normalize(weights: dict[str, float], lo: float, hi: float) -> dict[str, float]:
    """Scale by one common factor and clip to ``[lo, hi]`` so the weights sum to 1.

    Falls back to plain :func:`normalize` when the bounds cannot be met.
    """
    # every weight gets a tiny floor so a zero weight can still be scaled up to ``lo``
    base = {name: max(w, 1e-9) for name, w in normalize(weights).items()}
    n = len(base)
    lo, hi = min(lo, hi), max(lo, hi)
    if n * lo > 1.0 + 1e-12 or n * hi < 1.0 - 1e-12:
        return normalize(weights)

    def total(c: float) -> float:
        return sum(min(max(c * w, lo), hi) for w in base.values())

    low_c, high_c = 0.0, 1.0
    while total(high_c) < 1.0 and high_c < 1e12:
        high_c *= 2.0
    for _ in range(200):
        mid = (low_c + high_c) / 2.0
        if total(mid) < 1.0:
            low_c = mid
        else:
            high_c = mid
    clipped = {name: min(max(high_c * w, lo), hi) for name, w in base.items()}
    s = sum(clipped.values())
    return {name: value / s for name, value in clipped.items()}


def current_weights(state_weights: WeightsState, defaults: dict[str, float]) -> dict[str, float]:
    """The learned weights from state when complete and valid, else the defaults; normalised."""
    current = state_weights.current or {}
    values = [_finite_nonneg(current.get(name)) for name in FEATURE_NAMES]
    if all(v is not None for v in values) and sum(v for v in values if v is not None) > 0:
        return normalize(
            {name: float(v) for name, v in zip(FEATURE_NAMES, values, strict=True) if v is not None}
        )
    return normalize(defaults)


def update_weights(
    weights: dict[str, float], features: Features, label: float, settings: FeedbackSettings
) -> dict[str, float]:
    """One learning step for one labelled post (label in [-1, 1])."""
    w = normalize(weights)
    label = max(-1.0, min(1.0, float(label)))
    values = features.as_dict()
    mean_f = sum(values.values()) / len(values)
    stepped = {
        name: min(
            max(
                w[name] * (1.0 + settings.learning_rate * label * (values[name] - mean_f)),
                settings.weight_min,
            ),
            settings.weight_max,
        )
        for name in FEATURE_NAMES
    }
    return bounded_normalize(stepped, settings.weight_min, settings.weight_max)


def _drivers(name: str, labels: list[LabelExample]) -> tuple[float, float]:
    """How much 👍 posts (first) and 👎 posts (second) pushed ``name`` up (+) or down (-)."""
    up = down = 0.0
    for example in labels:
        values = example.features.as_dict()
        mean_f = sum(values.values()) / len(values)
        push = example.label * (values[name] - mean_f)
        if example.label > 0:
            up += push
        elif example.label < 0:
            down += push
    return up, down


def _explain_mover(name: str, diff: float, labels: list[LabelExample]) -> str | None:
    up, down = _drivers(name, labels)
    rising = diff > 0
    up_helps = up > 0 if rising else up < 0
    down_helps = down > 0 if rising else down < 0
    if not up_helps and not down_helps:
        return None
    if up_helps and (not down_helps or abs(up) >= abs(down)):
        # 👍 on high-f games raises the weight; 👍 on low-f games lowers it
        return f"You've been 👍-ing {(STRONG_PHRASES if rising else WEAK_PHRASES)[name]}"
    # 👎 on low-f games raises the weight; 👎 on high-f games lowers it
    return f"You've been 👎-ing {(WEAK_PHRASES if rising else STRONG_PHRASES)[name]}"


def describe_change(
    before: dict[str, float], after: dict[str, float], labels: list[LabelExample]
) -> str | None:
    """A short weekly note about the biggest weight change(s), or None if nothing moved.

    e.g. "You've been 👍-ing games with strong comment hype; hype weight went from 0.15 → 0.19"
    """
    b, a = normalize(before), normalize(after)
    diffs = sorted(((a[n] - b[n], n) for n in FEATURE_NAMES), key=lambda d: (-abs(d[0]), d[1]))
    top_diff, top = diffs[0]
    if abs(top_diff) < NOTE_MIN_CHANGE:
        return None
    lead = _explain_mover(top, top_diff, labels) or "Your 👍/👎 reactions shifted the weights"
    note = f"{lead}; {top} weight went from {b[top]:.2f} → {a[top]:.2f}"
    second_diff, second = diffs[1]
    if abs(second_diff) >= max(NOTE_MIN_CHANGE, abs(top_diff) / 2):  # only a comparable second mover
        note += f" (and {second} {b[second]:.2f} → {a[second]:.2f})"
    return note
