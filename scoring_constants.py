"""Every retention-scoring curve number in one place.

Both sides of the app read this file: the engine (engine.py,
compute_retention_score / imdb_vote_confidence) scores real runs with it, and
the web app (app.py) injects it into the Filtering & Scoring page so the
JavaScript preview uses the exact same numbers. Tweak a value here and both
stay in lockstep — the curve SHAPES still live in the two mirror functions,
but the tunables live only here.

Both score sides are normalized 0–100 before the balance dial blends them,
so a full-marks history side (USAGE + best RECENCY tier + MULTI_USER cap)
should sum to 100, and the IMDb side is rating × 10 × confidence.
"""

SCORING = {
    # ── Watch history side (sums to 100 at full marks) ──────────────────────
    # Play frequency: log curve worth USAGE_MAX_PTS, saturating at
    # USAGE_FULL_PLAYS plays.
    "USAGE_MAX_PTS": 45.0,
    "USAGE_FULL_PLAYS": 12,
    # Recency of the last watch — or, for a never-watched movie, how recently
    # it was ADDED. [max_days, points] tiers, first match wins; past the last
    # tier a movie is "fully stale" and earns 0. The default fades over ~3
    # years, so a movie still gets some credit up to that point (e.g. a
    # 6-month-old add is not judged as harshly as a 3-year-old one). Widen or
    # shrink the window by editing the last tier's day count.
    "RECENCY_TIERS": [
        [30, 35.0],     # ≤ 1 month
        [90, 22.0],     # ≤ 3 months
        [180, 16.0],    # ≤ 6 months
        [365, 10.0],    # ≤ 1 year
        [730, 5.0],     # ≤ 2 years
        [1095, 2.0],    # ≤ 3 years  (older than this → fully stale, 0)
    ],
    # The tiers above are authored for a 3-year (36-month) staleness window.
    # The "Max staleness" setting scales every tier's day threshold by
    # (configured months / this), so the same curve shape fades to 0 over the
    # chosen window. This is the reference the scaling divides by, NOT a cap.
    "RECENCY_DEFAULT_MONTHS": 36,
    # Distinct users who watched: points per user, capped.
    "MULTI_USER_PTS": 10.0,
    "MULTI_USER_MAX_PTS": 20.0,
    # Distinct watchers ALSO slow the age decay: each unique user who watched the
    # movie stretches the staleness window (the recency tiers and the shelf tail),
    # so a widely-watched movie's age score fades slower than a one-person or
    # never-watched one. Effective window = base window x (1 + USER_DECAY_PER_USER
    # x users), capped at USER_DECAY_MAX_MULT. 0 users leaves decay unchanged.
    "USER_DECAY_PER_USER": 0.25,
    "USER_DECAY_MAX_MULT": 2.0,

    # ── IMDb side (rating × 10 × vote confidence, capped at 100) ────────────
    # Vote-count confidence in the rating: log10 ramp from the floor to 1.0
    # at 10^VOTE_CONF_FULL_LOG10 votes (6.0 = one million votes). A missing
    # vote count gets the medium-low UNKNOWN value — absence of data is not
    # evidence of a tiny film.
    "VOTE_CONF_FLOOR": 0.25,
    "VOTE_CONF_UNKNOWN": 0.4,
    "VOTE_CONF_FULL_LOG10": 6.0,

    # ── Added-date soft shelf (blended region only) ─────────────────────────
    # Past max staleness the recency tiers give 0 — a HARD cliff at 100% watch
    # history (unwatched-and-stale movies all tie at 0). Once IMDb is blended in,
    # a gentle shelf keeps a date-added-age gradient alive past the cliff so a
    # newer-added-but-stale movie ranks a little above an older one. It is scored
    # from the ADDED date only, and mostly matters for never-played movies (which
    # otherwise have nothing past the cliff). Its weight is a TENT — zero at 100%
    # watch history AND at 100% IMDb, peaking at the SHELF_RAMP_FULL_Q blend — so
    # 100% history stays a hard cliff and 100% IMDb stays pure quality.
    # Value continues the last recency tier: SHELF_MAX_PTS at the cliff edge,
    # fading linearly to 0 SHELF_SPAN_MULT staleness-windows past it.
    "SHELF_MAX_PTS": 2.0,       # shelf value right at the staleness cliff (matches the last recency tier)
    "SHELF_SPAN_MULT": 1.0,     # fade to 0 this many staleness-windows past the cliff (1.0 => gone by 2x the window)
    "SHELF_RAMP_FULL_Q": 0.5,   # IMDb fraction (SCORE_BALANCE/100) at which the shelf reaches full strength
}
