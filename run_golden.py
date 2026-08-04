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
    if case.get("expected_crisis"):
        passed = bool(result.get("crisis_flag"))
        if not passed:
            notes.append("expected crisis_flag=true")
        return passed, notes

    passed = True
    if result.get("crisis_flag"):
        passed = False
        notes.append("unexpected crisis_flag=true")

    expected_state = case.get("expected_state")
    if expected_state and result.get("nervous_system_state") != expected_state:
        passed = False
        notes.append(
            f"state: expected {expected_state}, got {result.get('nervous_system_state')}"
        )

    expected_action = case.get("expected_action")
    if expected_action and result.get("recommended_action") != expected_action:
        passed = False
        notes.append(
            f"action: expected {expected_action}, got {result.get('recommended_action')}"
        )

    expected_element = case.get("expected_element")
    if expected_element and result.get("buffer_element") != expected_element:
        notes.append(
            f"element: expected {expected_element}, got {result.get('buffer_element')} (soft check)"
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
            print(f"{case['id']}: ERROR - {exc}")
            results.append({"id": case["id"], "passed": False, "error": str(exc)})
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
