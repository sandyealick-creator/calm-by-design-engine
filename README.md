# Calm by Design — Assessment Engine

AI-native adaptive routing service for the **Calm by Design** nervous system regulation program (White Raven Holistic). Built for the Build with Gemini XPRIZE, May–Aug 2026.

**What it does:** receives a client's daily check-in (journal + symptom scores) from GoHighLevel, assesses nervous system state with Gemini (structured JSON output), and executes the curriculum decision — advance, hold, or route to a Somatic Safety Buffer — writing every record and state transition to Airtable. Crisis language halts all AI routing and alerts a human immediately.

```
GHL form ──▶ POST /assess ──▶ Daily Log (Airtable)
                   │
                   ├──▶ Gemini (structured output)
                   │         │
                   ├──▶ AI Assessment record (+ latency/token telemetry)
                   │
                   ├──▶ State machine ──▶ Client update + State Transition audit row
                   │
                   └──▶ GHL webhook ──▶ delivers module or buffer protocol
```

## Status

- [x] Airtable base ("CBD Core") built and populated
- [x] Gemini assessment engine (`main.py`) written and deployed
- [x] Deployed to Google Cloud Run (`cbd-assess`, `us-east1`)
- [x] End-to-end test verified: journal → Gemini classification → Airtable records → state transition
- [x] Credentials rotated after being briefly exposed in a shared doc during setup
- [ ] GitHub repo created and pushed *(this repo)*
- [ ] GoHighLevel inbound/outbound webhooks wired
- [ ] Golden test set run and results archived
- [ ] Devpost submission finalized

## Local setup (for reference — do not commit real credentials)

Credentials are supplied to Cloud Run via `--set-env-vars` / `--update-env-vars` at deploy time, or via a local `env.yaml` that is excluded from version control (see `.gitignore`). Required variables:

- `GEMINI_API_KEY` — from [aistudio.google.com](https://aistudio.google.com)
- `AIRTABLE_API_KEY` — Airtable personal access token, scoped to the CBD Core base only (`data.records:read`, `data.records:write`)
- `WEBHOOK_SECRET` — a long random string; GHL must send this in the `X-Webhook-Secret` header on every request
- `GEMINI_MODEL` — the Gemini model string in use

## Deploy

```
gcloud run deploy cbd-assess \
  --source . \
  --region us-east1 \
  --allow-unauthenticated \
  --set-env-vars "GEMINI_API_KEY=...,AIRTABLE_API_KEY=...,WEBHOOK_SECRET=..."
```

`--allow-unauthenticated` is safe here because every request must still carry a valid `X-Webhook-Secret` header — requests without it are rejected by the app itself.

## Test

```
pip install -r requirements.txt
export GEMINI_API_KEY=your_key
python run_golden.py
```

`golden_set.json` contains 15 test journals covering every nervous-system state, an ambiguous case (must HOLD, never guess ADVANCE), a journal-vs-scores conflict, and two crisis cases that must flag. Results are evidence for judges.

## Where the evidence lives (for the submission)

- **AI executes key decisions:** State Transitions table (Airtable) + Cloud Run logs
- **Telemetry:** Cloud Console → Cloud Run → cbd-assess → Logs (every assessment logs latency, tokens, confidence, model)
- **Costs:** Cloud Console Billing + Airtable/GHL invoices → cost ledger

## Safety design

- Crisis language (self-harm, harm to others, medical emergency) halts all AI routing and alerts a human. The AI never counsels.
- Uncertainty defaults to HOLD, never ADVANCE.
- This software supports a wellness curriculum; it does not diagnose or treat any condition.
