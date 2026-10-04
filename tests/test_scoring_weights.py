"""Weights: defaults vs learned, the 👍/👎 update rule, bounds, and the weekly note."""

from __future__ import annotations

import random

import pytest

from gembot.config import FeedbackSettings
from gembot.models import FEATURE_NAMES, Features, LabelExample, WeightsState
from gembot.scoring.weights import (
    bounded_normalize,
    current_weights,
    describe_change,
    normalize,
    update_weights,
)
from tests.factories import NOW, make_config

DEFAULTS = make_config().settings.weights
FEEDBACK = FeedbackSettings()

HIGH_HYPE = Features(velocity=0.2, underdog=0.2, cross=0.0, fit=0.5, hype=0.95, meme=0.0, fresh=0.5)


def label(value: float, features: Features = HIGH_HYPE) -> LabelExample:
    return LabelExample(at=NOW, message_id="m", game_id="g", label=value, features=features)


# ----------------------------------------------------------------------------- normalize / current


def test_normalize_fills_missing_and_drops_bad_values():
    w = normalize(
        {"velocity": 2.0, "hype": 2.0, "bogus": 9.0, "fresh": -1.0, "fit": float("nan"), "meme": "x"}
    )
    assert list(w) == list(FEATURE_NAMES)
    assert w["velocity"] == w["hype"] == 0.5
    assert w["fresh"] == w["fit"] == w["meme"] == 0.0
    assert normalize({}) == {name: pytest.approx(1 / 7) for name in FEATURE_NAMES}


def test_current_weights_prefers_valid_learned_weights():
    learned = {name: 1.0 for name in FEATURE_NAMES}
    assert current_weights(WeightsState(current=learned), DEFAULTS) == {
        n: pytest.approx(1 / 7) for n in FEATURE_NAMES
    }
    assert current_weights(WeightsState(), DEFAULTS) == pytest.approx(DEFAULTS)
    incomplete = {"velocity": 1.0}
    assert current_weights(WeightsState(current=incomplete), DEFAULTS) == pytest.approx(DEFAULTS)
    broken = dict(learned, hype=float("inf"))
    assert current_weights(WeightsState(current=broken), DEFAULTS) == pytest.approx(DEFAULTS)
    zeros = {name: 0.0 for name in FEATURE_NAMES}
    assert current_weights(WeightsState(current=zeros), DEFAULTS) == pytest.approx(DEFAULTS)
    doubled = {k: 2 * v for k, v in DEFAULTS.items()}
    assert sum(current_weights(WeightsState(current=doubled), DEFAULTS).values()) == pytest.approx(1.0)


# ----------------------------------------------------------------------------- update rule


def test_update_rule_matches_spec_formula_when_no_bound_is_hit():
    f = HIGH_HYPE
    values = f.as_dict()
    mean_f = sum(values.values()) / 7
    raw = {n: DEFAULTS[n] * (1 + 0.05 * 1.0 * (values[n] - mean_f)) for n in FEATURE_NAMES}
    expected = {n: v / sum(raw.values()) for n, v in raw.items()}
    assert update_weights(DEFAULTS, f, 1.0, FEEDBACK) == pytest.approx(expected)


def test_thumbs_up_on_high_hype_game_raises_hype_weight():
    after = update_weights(DEFAULTS, HIGH_HYPE, 1.0, FEEDBACK)
    assert after["hype"] > DEFAULTS["hype"]
    assert after["meme"] < DEFAULTS["meme"]
    assert sum(after.values()) == pytest.approx(1.0)


def test_thumbs_down_on_high_hype_game_lowers_hype_weight():
    after = update_weights(DEFAULTS, HIGH_HYPE, -1.0, FEEDBACK)
    assert after["hype"] < DEFAULTS["hype"]


def test_zero_label_changes_nothing_and_labels_are_clamped():
    assert update_weights(DEFAULTS, HIGH_HYPE, 0.0, FEEDBACK) == pytest.approx(DEFAULTS)
    assert update_weights(DEFAULTS, HIGH_HYPE, 5.0, FEEDBACK) == pytest.approx(
        update_weights(DEFAULTS, HIGH_HYPE, 1.0, FEEDBACK)
    )


def test_many_thumbs_up_saturate_at_the_upper_bound():
    w = dict(DEFAULTS)
    for _ in range(3000):
        w = update_weights(w, HIGH_HYPE, 1.0, FEEDBACK)
    # hype is clipped at the cap each step, then everything is scaled to sum to 1, so it
    # settles just under 0.5 while the other above-average features share the rest
    assert 0.45 < w["hype"] <= FEEDBACK.weight_max
    assert w["velocity"] == w["meme"] == pytest.approx(FEEDBACK.weight_min)
    assert min(w.values()) >= FEEDBACK.weight_min - 1e-9
    assert sum(w.values()) == pytest.approx(1.0)


def test_weights_stay_normalized_and_bounded_after_random_updates():
    rng = random.Random(7)
    w = dict(DEFAULTS)
    for _ in range(2000):
        features = Features(**{n: rng.random() for n in FEATURE_NAMES})
        w = update_weights(w, features, rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0]), FEEDBACK)
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-9)
        assert all(FEEDBACK.weight_min - 1e-9 <= v <= FEEDBACK.weight_max + 1e-9 for v in w.values())


def test_aggressive_learning_rate_still_respects_bounds():
    fast = FeedbackSettings(learning_rate=5.0)
    w = update_weights(DEFAULTS, Features(hype=1.0), 1.0, fast)
    assert w["hype"] == pytest.approx(0.5)
    assert all(v >= fast.weight_min - 1e-9 for v in w.values())
    assert sum(w.values()) == pytest.approx(1.0)


def test_bounded_normalize_edge_cases():
    # a single non-zero weight cannot exceed the cap; the zeros are lifted to the floor
    w = bounded_normalize({"velocity": 1.0}, 0.02, 0.5)
    assert w["velocity"] == pytest.approx(0.5)
    assert sum(w.values()) == pytest.approx(1.0)
    assert min(w.values()) >= 0.02 - 1e-9
    # infeasible bounds (7 * 0.2 > 1) fall back to a plain normalisation
    assert bounded_normalize(DEFAULTS, 0.2, 0.5) == pytest.approx(DEFAULTS)
    assert bounded_normalize(DEFAULTS, 0.5, 0.02) == pytest.approx(DEFAULTS)


# ----------------------------------------------------------------------------- weekly note


def test_describe_change_matches_spec_example():
    after = {
        "velocity": 0.24,
        "underdog": 0.14,
        "cross": 0.14,
        "fit": 0.14,
        "hype": 0.19,
        "meme": 0.05,
        "fresh": 0.10,
    }
    note = describe_change(DEFAULTS, after, [label(1.0)])
    assert note == "You've been 👍-ing games with strong comment hype; hype weight went from 0.15 → 0.19"


def test_describe_change_normalizes_inputs():
    after = dict(DEFAULTS, hype=0.19)  # sums to 1.04 -> hype is really 0.18
    note = describe_change(DEFAULTS, after, [label(1.0)])
    assert note == "You've been 👍-ing games with strong comment hype; hype weight went from 0.15 → 0.18"


def test_describe_change_directions():
    low_hype = Features(velocity=0.9, underdog=0.9, cross=0.6, fit=0.9, hype=0.0, meme=0.2, fresh=1.0)
    up = dict(DEFAULTS, hype=0.19)
    down = dict(DEFAULTS, hype=0.11)
    assert describe_change(DEFAULTS, up, [label(-1.0, low_hype)]).startswith(
        "You've been 👎-ing games with little comment hype"
    )
    assert describe_change(DEFAULTS, down, [label(-1.0)]).startswith(
        "You've been 👎-ing games with strong comment hype"
    )
    assert describe_change(DEFAULTS, down, [label(1.0, low_hype)]).startswith(
        "You've been 👍-ing games with little comment hype"
    )
    assert describe_change(DEFAULTS, up, []).startswith("Your 👍/👎 reactions shifted the weights; hype")
    # the reactions point the other way (e.g. weights were edited by hand): generic wording
    assert describe_change(DEFAULTS, up, [label(-1.0)]).startswith("Your 👍/👎 reactions shifted")


def test_describe_change_mentions_second_mover_and_ignores_noise():
    after = dict(DEFAULTS, velocity=0.22, hype=0.21, fresh=0.07)
    note = describe_change(DEFAULTS, after, [label(1.0)])
    assert note.endswith("hype weight went from 0.15 → 0.21 (and fresh 0.10 → 0.07)")
    assert describe_change(DEFAULTS, dict(DEFAULTS), [label(1.0)]) is None
    tiny = dict(DEFAULTS, hype=0.1530)
    assert describe_change(DEFAULTS, tiny, [label(1.0)]) is None


def test_weekly_note_after_real_updates():
    w = dict(DEFAULTS)
    labels = [label(1.0) for _ in range(30)]
    for example in labels:
        w = update_weights(w, example.features, example.label, FEEDBACK)
    note = describe_change(DEFAULTS, w, labels)
    assert note is not None and "👍-ing games with strong comment hype; hype weight went from 0.15 →" in note
