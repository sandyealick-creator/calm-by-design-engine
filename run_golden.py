"""
Golden set evaluation harness for the CBD Gemini assessment engine.
Run after any system_prompt.txt change: python run_golden.py
Requires GEMINI_API_KEY in the environment. Writes golden_results.json.
"""
import json
import sys

from main import run_assessment

GOLDEN_PATH = "golden_set.json"
RESULTS_PATH = "golden_results.json"


def check_case(case, result):
    """Returns (passed, notes) for one golden case."""
    notes = []
    passed = True

    expected_safety = case.get("expected_safety_signal")
    if expected_safety and result.get("safety_signal") != expected_safety:
        passed = False
        notes.append(
            f"safety_signal: expected {expected_safety}, got {result.get('safety_signal')}"
        )

    expected_sentiment = case.get("expected_sentiment")
    if expected_sentiment and result.get("sentiment") != expected_sentiment:
        passed = False
        notes.append(
            f"sentiment: expected {expected_sentiment}, got {result.get('sentiment')}"
        )

    if case.get("expected_progress_signal") is not None:
        if bool(result.get("progress_signal")) != case["expected_progress_signal"]:
            passed = False
            notes.append(
                f"progress_signal: expected {case['expected_progress_signal']}, "
                f"got {result.get('progress_signal')}"
            )

    if case.get("expected_distress_signal") is not None:
        if bool(result.get("distress_signal")) != case["expected_distress_signal"]:
            passed = False
            notes.append(
                f"distress_signal: expected {case['expected_distress_signal']}, "
                f"got {result.get('distress_signal')}"
            )

    expected_element = case.get("expected_element")
    if expected_element and result.get("suggested_element") != expected_element:
        notes.append(
            f"element: expected {expected_element}, got {result.get('suggested_element')} (soft check)"
        )

    return passed, notes


def main():
    with open(GOLDEN_PATH) as f:
        cases = json.load(f)

    results = []
    failures = 0

    for case in cases:
        context = {"current_week": 1, "current_state": "On Track", "prior_assessments": []}
        try:
            result, latency_ms, tok_in, tok_out = run_assessment(
                case["journal_text"], case["scores"], context
            )
        except Exception as exc:
            error_type = type(exc).__name__
            print(f"{case['id']}: ERROR - {error_type}")
            results.append({"id": case["id"], "passed": False,
                            "error_type": error_type})
            failures += 1
            continue

        passed, notes = check_case(case, result)
        status = "PASS" if passed else "FAIL"
        print(f"{case['id']}: {status}"
              f"{' - ' + '; '.join(notes) if notes else ''}")

        results.append({
            "id": case["id"],
            "passed": passed,
            "notes": notes,
            "result": result,
            "latency_ms": latency_ms,
            "tokens_in": tok_in,
            "tokens_out": tok_out,
        })
        if not passed:
            failures += 1

    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)

    total = len(cases)
    print(f"\n{total - failures}/{total} passed. Full results in {RESULTS_PATH}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
