"""
Calm by Design - routing configuration
=======================================
Central, non-clinical support-routing thresholds and the five response
routes. Change numbers here, not in main.py's routing logic.
"""

# ---------------------------------------------------------------------------
# C. Support score
# ---------------------------------------------------------------------------
def support_score(physical_symptoms, anxiety, energy, sleep):
    """Deterministic, non-clinical support-routing score. Range 4-40.
    Higher = greater current need for support. Not a diagnosis, medical
    assessment, suicide-risk score, or validated mental-health instrument.
    """
    return physical_symptoms + anxiety + (11 - energy) + (11 - sleep)


# score <= bound -> tier, checked in order
SCORE_TIERS = [
    (11, "POSITIVE_REGULATED"),
    (15, "STEADY"),
    (23, "GROUNDING_SUPPORT"),
    (40, "HEIGHTENED_SUPPORT"),
]

TIER_LABELS = {
    "POSITIVE_REGULATED": "Positive/Regulated",
    "STEADY": "Steady",
    "GROUNDING_SUPPORT": "Grounding Support",
    "HEIGHTENED_SUPPORT": "Heightened Support",
}


def score_tier(score):
    for bound, tier in SCORE_TIERS:
        if score <= bound:
            return tier
    return SCORE_TIERS[-1][1]


# ---------------------------------------------------------------------------
# E. Response routes
# ---------------------------------------------------------------------------
ROUTE_POSITIVE_PROGRESS = "POSITIVE_PROGRESS"
ROUTE_STEADY = "STEADY"
ROUTE_GROUNDING_SUPPORT = "GROUNDING_SUPPORT"
ROUTE_HEIGHTENED_SUPPORT = "HEIGHTENED_SUPPORT"
ROUTE_SAFETY = "SAFETY_ROUTE"

# Medical-emergency override: a separate safety override, not a sixth
# wellness-score tier. It is intentionally NOT handled inside route() below -
# route()'s five outputs and logic are unchanged. main.py's process_checkin
# checks for a medical emergency signal BEFORE calling route(), and only
# calls route() when there is none (or defers to ROUTE_SAFETY when self-harm
# language is also present - self-harm and medical-emergency signals are
# independent and can co-occur).
ROUTE_MEDICAL_EMERGENCY = "MEDICAL_EMERGENCY_ROUTE"

ROUTE_LABELS = {
    ROUTE_POSITIVE_PROGRESS: "Positive/Progress",
    ROUTE_STEADY: "Steady",
    ROUTE_GROUNDING_SUPPORT: "Grounding Support",
    ROUTE_HEIGHTENED_SUPPORT: "Heightened Support",
    ROUTE_SAFETY: "Safety Route",
    ROUTE_MEDICAL_EMERGENCY: "Medical Emergency Route",
}

# Curriculum reentry / pacing (unchanged from the original state machine,
# just centralized here alongside the rest of the routing config)
REENTRY_THRESHOLD = 2   # consecutive Regulated-equivalent days to exit Safety Buffer
FINAL_WEEK = 10
MIN_DAYS_BETWEEN_ADVANCES = 7  # curriculum may advance at most once per this many days


def route(score_tier_value, safety_signal_triggered, distress_signal, progress_signal):
    """Combine the deterministic score tier with Gemini/keyword safety and
    distress/progress signals into exactly one of the five response routes.
    Safety is independent of the score and always wins.
    """
    if safety_signal_triggered:
        return ROUTE_SAFETY

    if score_tier_value == "GROUNDING_SUPPORT" or (distress_signal and score_tier_value in
                                                     ("POSITIVE_REGULATED", "STEADY")):
        return ROUTE_GROUNDING_SUPPORT

    if score_tier_value == "HEIGHTENED_SUPPORT":
        return ROUTE_HEIGHTENED_SUPPORT

    if score_tier_value == "POSITIVE_REGULATED" and progress_signal:
        return ROUTE_POSITIVE_PROGRESS

    if score_tier_value == "POSITIVE_REGULATED":
        return ROUTE_STEADY

    return ROUTE_STEADY
