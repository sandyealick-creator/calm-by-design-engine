# Calm by Design — Assessment Engine

AI-native adaptive routing service for the **Calm by Design** nervous system regulation program (White Raven Holistic). Built for the Build with Gemini XPRIZE, May–Aug 2026.

**What it does:** lets a participant enroll once, then check in daily (symptom ratings + journal) through a branded web page. A deterministic, non-clinical support score plus a Gemini structured classification (sentiment, distress/progress signals, safety signal) decide one of five response routes — Positive/Progress, Steady, Grounding Support, Heightened Support, or the Safety Route — and write the check-in and routing audit trail to Airtable when persistence is available. Direct self-harm or imminent-danger language is checked by a narrow deterministic backstop before external processing, and Gemini also classifies submissions on the normal dependency-available path. A triggering result shows immediate on-screen 988/911 guidance and makes a best-effort Crisis Alert record when persistence is available, independent of the score. Record creation does not prove notification delivery or human review. The original GoHighLevel form + webhook path (`/assess`) continues to use the same underlying assessment logic.

```
Participant browser                              GoHighLevel (existing, unchanged)
  /enroll ──▶ Client record (Airtable)              GHL form ──▶ POST /assess ──┐
  /access#t=... ──▶ same-origin POST ──▶ session cookie                        │
  /checkin ──▶──────────────────────────────────────────────────────────────────┤
                                                                                 ▼
                                                            process_checkin() (shared core)
                                                                 │
                                                                 ├──▶ Daily Log (Airtable)
                                                                 │
                                                                 ├──▶ Gemini structured output
                                                                 │      + deterministic safety rule
                                                                 │      (normal eligible assessment path)
                                                                 │
                                                                 ├──▶ support score + tier
                                                                 │      (routing_config.py, central config)
                                                                 │
                                                                 ├──▶ response route (5-way) ──▶ AI Assessment
                                                                 │                                 (Airtable)
                                                                 ├──▶ curriculum state machine
                                                                 │      ──▶ Client update + State Transition
                                                                 │
                                                                 └──▶ Safety Route ──▶ Crisis Alert (Airtable)
                                                                        ──▶ existing GHL Crisis Alert workflow
```

GHL's Crisis Alert Notification and Safety Buffer Routing workflows continue to poll Airtable directly for new Crisis Alert / AI Assessment records exactly as before — that delivery mechanism is unchanged by this revision. `GHL_ROUTING_WEBHOOK`/`GHL_CRISIS_WEBHOOK` remain optional outbound calls the app makes if those env vars are set; they are not the primary delivery path.

## Status

- [x] Airtable base ("CBD Core") built and populated, extended with participant/consent, scoring, and routing audit fields
- [x] Gemini assessment engine (`main.py`) rewritten around a deterministic support score + 5-route model, with a shared core used by both the new web pages and the existing GHL webhook
- [x] Participant-facing enrollment, check-in, recovery, and result/safety pages (`templates/`)
- [ ] Deploy and verify this version on Google Cloud Run (`cbd-assess`, `us-east1`)
- [ ] Complete controlled end-to-end verification with test-only records before participant enrollment
- [x] Credentials rotated after being briefly exposed in a shared doc during setup
- [x] GitHub repo created and pushed *(this repo)*
- [x] Existing GHL "Daily Check-In - Assessment" webhook workflow retained against `/assess`; controlled testing must verify its payload against the new length, rate, and optional submission-ID validation
- [ ] New GHL "Send Check-in Link" workflow (Recovery Requests polling) — manual, see `MANUAL_TEST_CHECKLIST.md`
- [x] Golden test set run and results archived (`golden_results.json`)
- [ ] Devpost submission finalized

## Local setup (for reference — do not commit real credentials)

Required runtime variable names are listed below. Deployment configuration must
use approved Secret Manager references by name or resource reference only. Do
not put plaintext credential values in commands, documentation, terminal output,
or release records.

- `GEMINI_API_KEY` — from [aistudio.google.com](https://aistudio.google.com)
- `AIRTABLE_API_KEY` — Airtable personal access token, scoped to the CBD Core base only (`data.records:read`, `data.records:write`)
- `WEBHOOK_SECRET` — a long random string; GHL must send this in the `X-Webhook-Secret` header on every request
- `SESSION_SECRET` — a long random string used to generate CSRF tokens for the participant-facing pages; never in source
- `GEMINI_MODEL` — the Gemini model string in use

## Deploy

Do not deploy directly from source or a mutable image. Follow
[`DEPLOYMENT_RUNBOOK.md`](DEPLOYMENT_RUNBOOK.md) only after separate explicit
authorization. It requires an exact clean Git commit, an authorized build, an
immutable resulting image digest, explicit project/region/service/repository/
image identity, and a candidate revision created with zero traffic. Live
verification and traffic movement require separate authorizations. Secret
Manager references may be recorded by name or resource reference only, never as
plaintext credential values in commands, documentation, terminal output, or the
release record. The corrected procedure has not been executed against Cloud.

`scripts/validate_deployment_state.py` and
`tests/test_deployment_state.py` provide local, fixture-based validation for
strict revision, traffic, zero-traffic transition, `SESSION_SECRET` reference,
and secret-version metadata evidence. They also validate authorized evidence
scope, exact candidate-tag and revision nonexistence, terminal build/source/image
binding, Artifact Registry DockerImage resolution, safe runtime hashes, and
complete authorized traffic or rollback maps. A completed Build must declare the
exact candidate tag in `images[]` and report exactly one matching
`results.images[]` BuiltImage with a canonical digest, exact Artifact Registry
Package resource, and a structurally valid optional `pushTiming`/OCI media type.
When present, `pushTiming` must be fully contained within the Build execution
interval at nanosecond precision with timezone-offset normalization. The exact,
non-paginated Artifact Registry `DockerImages.Get` resource is constructed from
the validated candidate tag and Build digest; its DockerImage URI digest and tag
must agree before the validator derives a deployable immutable reference. This is
Cloud Build artifact-output binding, not a SLSA attestation. Saved
`SESSION_SECRET` results are strictly rebound to the current project, region,
and service before metadata URLs are constructed, and the secret must resolve
within the current project with an exact positive numeric version. Aliases and
plaintext-like selectors fail before every HTTP classification. Runtime
preservation compares a schema-validated safe Cloud Run v2 projection with a
nonempty named-container structure; it deliberately excludes plaintext
environment and probe-header values and does not claim equality of unselected
fields. A floating `LATEST` allocation is a
deployment and rollback hazard, not a standalone source-validation or image-build
blocker. The governed procedure records both the gcloud-emitted map and a resolved effective
serving map: an authorized digest-based `--no-traffic` deployment may convert
floating `LATEST` to its currently bound fixed revision, but it must leave the
effective serving allocation unchanged and the candidate at zero. Build,
candidate deployment, smoke/integration testing, each fixed-revision traffic
movement, rollback, and participant enrollment remain separate authorization
boundaries.

The participant enrollment, recovery, token-redemption, and check-in routes are
intentionally public at the Cloud Run layer and enforce their own cookie, CSRF,
token, and rate-limit controls. Only the machine-facing `/assess` route requires
the `X-Webhook-Secret`. Controlled deployment testing must verify Cloud Run
request-log behavior, proxy addressing, cookies, and all required environment
variables before participant enrollment.

## Test

```
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python scripts/run_mocked_tests.py -p no:cacheprovider tests
```

Do not run the golden set without separate authorization to use the API budget.
Provide `GEMINI_API_KEY` through an approved secure local mechanism that does
not place its value in documentation, commands, or shell history, then run
`python run_golden.py`.

The mocked-suite runner installs its guard before importing pytest, overrides
integration settings with test-only placeholders, and guards the Python test
process at non-loopback `getaddrinfo`, `socket.create_connection`,
`socket.socket.connect`, and `socket.socket.connect_ex`. This covers external
numeric IPv4 and IPv6 connections through those paths. It is not an operating-
system network sandbox and does not comprehensively prevent unconnected UDP,
native-extension, child-process, or every alternative networking path. The
Phase 2A run recorded zero guarded outbound attempts across 266 tests, with no
active bypass observed in the repository suite. Linux/container and stronger
OS-level isolation remain controlled verification or future-hardening items.
`requirements.txt` is the production-only hash lock; `requirements-dev.txt`
adds the separately resolved test dependencies.

`golden_set.json` contains test journals covering every response route, score boundary values, positive/neutral/distressed entries, and negated/historical/third-person/direct/imminent safety language. Results are evidence for judges. See `MANUAL_TEST_CHECKLIST.md` for the manual pass covering mobile layout and real GHL/Airtable round-trips against test records.

## Where the evidence lives (for the submission)

- **AI executes key decisions:** AI Assessments + State Transitions tables (Airtable) + Cloud Run logs
- **Telemetry:** Cloud Console → Cloud Run → cbd-assess → Logs (completed ordinary Gemini assessment paths log latency, tokens, tier, route, and model; emergency short-circuits and incomplete persistence paths do not necessarily emit that telemetry, and logs must never contain journal text, email, phone, or tokens)
- **Costs:** Cloud Console Billing + Airtable/GHL invoices → cost ledger

## Safety design

- The support score is a deterministic, non-clinical routing signal (4-40) — never a diagnosis, medical assessment, suicide-risk score, or validated instrument.
- Normal eligible assessment paths use Gemini's structured `safety_signal` when Gemini is available, combined with a narrow deterministic keyword rule (`safety_rules.py`) so the strongest signal wins. Validation, replay, rate-limit, and deterministic emergency short-circuits may finish without calling Gemini. The keyword rule remains available if Gemini is unavailable or returns an invalid response (`Fallback Mode` is recorded on processed fallback assessments).
- The Safety Route is independent of the score — a high score alone never classifies someone as suicidal, and it always shows 988/911 resources first, states plainly that the app is not emergency care and is not monitored in real time, and offers a grounding element only as an additional option afterward, never a replacement.
- This software supports a wellness curriculum; it does not diagnose or treat any condition. See `MANUAL_TEST_CHECKLIST.md` for what remains unsuitable for real clinical/crisis use.
