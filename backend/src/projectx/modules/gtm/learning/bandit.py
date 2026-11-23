"""Thompson sampling: try options in proportion to the chance each one is the best.

Arms with little data get explored; arms that keep winning get most of the traffic. Used to pick
hooks, themes and posting hours (continuous scores) and A/B variants (conversion rates).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence


def gaussian_pick(
    candidates: Sequence[str],
    stats: Mapping[str, tuple[int, float]],
    rng: random.Random,
    *,
    prior_mean: float = 0.5,
    prior_sd: float = 0.25,
) -> str:
    """stats: key -> (observations, mean score in 0..1)."""
    best, best_draw = candidates[0], -math.inf
    for key in candidates:
        n, mean = stats.get(key, (0, prior_mean))
        sd = prior_sd / math.sqrt(n + 1)
        draw = rng.gauss(mean if n else prior_mean, sd)
        if draw > best_draw:
            best, best_draw = key, draw
    return best


def beta_pick(arms: Mapping[str, tuple[int, int]], rng: random.Random) -> str:
    """arms: key -> (trials, successes)."""
    best, best_draw = next(iter(arms)), -1.0
    for key, (trials, wins) in arms.items():
        draw = rng.betavariate(1 + wins, 1 + max(trials - wins, 0))
        if draw > best_draw:
            best, best_draw = key, draw
    return best


def prob_best(arms: Mapping[str, tuple[int, int]], *, samples: int = 4000, seed: int = 11) -> dict[str, float]:
    """Monte Carlo probability that each arm has the highest true conversion rate."""
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    wins = dict.fromkeys(arms, 0)
    for _ in range(samples):
        draws = {k: rng.betavariate(1 + s, 1 + max(t - s, 0)) for k, (t, s) in arms.items()}
        wins[max(draws, key=lambda k: draws[k])] += 1
    return {k: v / samples for k, v in wins.items()}
