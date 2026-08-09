import routing_config as rc


def test_support_score_formula():
    assert rc.support_score(physical_symptoms=5, anxiety=5, energy=5, sleep=5) == 5 + 5 + 6 + 6
    assert rc.support_score(1, 1, 10, 10) == 1 + 1 + 1 + 1  # minimum: 4
    assert rc.support_score(10, 10, 1, 1) == 10 + 10 + 10 + 10  # maximum: 40


def test_score_tier_boundaries_11_12():
    assert rc.score_tier(11) == "POSITIVE_REGULATED"
    assert rc.score_tier(12) == "STEADY"


def test_score_tier_boundaries_15_16():
    assert rc.score_tier(15) == "STEADY"
    assert rc.score_tier(16) == "GROUNDING_SUPPORT"


def test_score_tier_boundaries_23_24():
    assert rc.score_tier(23) == "GROUNDING_SUPPORT"
    assert rc.score_tier(24) == "HEIGHTENED_SUPPORT"


def test_score_tier_full_range():
    assert rc.score_tier(4) == "POSITIVE_REGULATED"
    assert rc.score_tier(40) == "HEIGHTENED_SUPPORT"


def test_safety_route_wins_regardless_of_score():
    # A low score with a safety signal must still route to safety.
    assert rc.route("POSITIVE_REGULATED", True, False, False) == rc.ROUTE_SAFETY
    # A maximal score with no safety signal must NOT be classified as safety -
    # a high support score alone never implies suicide risk.
    assert rc.route("HEIGHTENED_SUPPORT", False, False, False) == rc.ROUTE_HEIGHTENED_SUPPORT


def test_grounding_support_from_tier():
    assert rc.route("GROUNDING_SUPPORT", False, False, False) == rc.ROUTE_GROUNDING_SUPPORT


def test_distress_at_lower_score_escalates_to_grounding():
    assert rc.route("STEADY", False, True, False) == rc.ROUTE_GROUNDING_SUPPORT
    assert rc.route("POSITIVE_REGULATED", False, True, False) == rc.ROUTE_GROUNDING_SUPPORT


def test_positive_progress_requires_progress_signal():
    assert rc.route("POSITIVE_REGULATED", False, False, True) == rc.ROUTE_POSITIVE_PROGRESS
    assert rc.route("POSITIVE_REGULATED", False, False, False) == rc.ROUTE_STEADY


def test_heightened_support_from_tier():
    assert rc.route("HEIGHTENED_SUPPORT", False, False, False) == rc.ROUTE_HEIGHTENED_SUPPORT
