# Agent instructions for this repository

Permanent ground rules for any coding agent (Codex, Claude Code, or otherwise) working in this repository. These apply to every task, not just the one you were given, unless the project owner explicitly overrides one in writing for a specific change.

## Product identity

- This is Calm by Design, a nervous system regulation program for White Raven Holistic, built for the Build with Gemini XPRIZE hackathon.
- Gemini remains the runtime AI engine. Do not migrate the runtime to OpenAI or another model provider unless explicitly authorized, and only after the hackathon concludes.
- Use "member" or "participant" in all copy and code comments. Never "patient."
- Never describe the support score or the AI's structured classification as clinical, diagnostic, validated, or a medical assessment. It is a non-clinical routing signal.
- Do not use em dashes in participant-facing copy (templates, emails, on-screen text). Use commas, periods, or restructure the sentence instead.

## Architecture

- Preserve the Flask + Cloud Run + Airtable + GHL architecture unless a change is explicitly approved. Do not introduce a new framework, a new database, or a new hosting target as a side effect of an unrelated task.
- The five wellness response routes (Positive/Progress, Steady, Grounding Support, Heightened Support, Safety Route) and the medical-emergency override are architecturally distinct. Keep them that way:
  - Wellness routing lives in `routing_config.route()`, driven by the deterministic support score tier plus Gemini's distress/progress signals.
  - The self-harm/suicide safety override and the medical-emergency override are both independent of the wellness tiers and of each other. Never infer one from the other, and never fold the medical-emergency check into `route()`'s tier logic - it is deliberately a separate check in `main.py`'s `process_checkin()`.
  - Preserve all five grounding elements: Earth, Air, Fire, Water, Spirit. Don't drop or consolidate one, and don't let a code change silently make one unreachable.
- Curriculum pacing: never advance the curriculum from a single positive journal entry. The `MIN_DAYS_BETWEEN_ADVANCES` gate in `routing_config.py` and the `Week Started At` field exist specifically to prevent this. If you touch curriculum logic, preserve the "at most one advance per scheduled week" invariant.
- Check-in submissions: allow multiple legitimate check-ins per participant per day. Prevent only accidental duplicate submissions, via the client-generated `submission_id` idempotency key, never by blocking a second same-day entry outright.

## Safety and data handling

- Never expose, print, log, copy, or commit credentials (API keys, tokens, secrets) or participant data (names, emails, phone numbers, journal text, raw Gemini responses containing any of the above). This includes not pasting them into commit messages, PR descriptions, or code comments.
- Do not modify live Airtable schema, live GoHighLevel workflows, or deploy to Cloud Run without the project owner's explicit approval for that specific change, even if the change seems obviously additive or safe.
- Airtable schema changes must be additive only (new fields/tables) unless the owner has separately and explicitly approved a destructive change (rename, retype, delete).
- Run the relevant test suite (`pytest`, and `python run_golden.py` when you have a real `GEMINI_API_KEY` available and the owner's go-ahead to spend the API call budget) before and after any material change, especially anything touching `routing_config.py`, `safety_rules.py`, `system_prompt.txt`, or the routing logic in `main.py`'s `process_checkin()`.

## Scope discipline

- Do not expand scope beyond the requested task. If you notice an unrelated bug or improvement opportunity while working, note it (in your response, or in `HANDOFF.md`'s known-limitations section) rather than fixing it unprompted.
- This is a hackathon MVP, not a production clinical system. Resist the urge to add clinical-sounding features, additional "safety" heuristics, or scoring systems beyond what's been explicitly requested - see `HANDOFF.md` for what's already been deliberately kept narrow and why.
