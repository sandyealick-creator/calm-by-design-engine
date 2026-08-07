"""
Calm by Design - Assessment Engine (Cloud Run)
================================================
Single webhook hub for the CBD adaptive curriculum.

Flow:  GHL check-in form -> POST /assess -> Daily Log (Airtable)
       -> Gemini structured assessment -> AI Assessment (Airtable)
       -> state machine -> Client update + State Transition (Airtable)
       -> outbound GHL webhook (module or buffer delivery)

Env vars required (set on Cloud Run):
  GEMINI_API_KEY        Google AI Studio key
  AIRTABLE_API_KEY      Airtable personal access token
  WEBHOOK_SECRET        shared secret; GHL must send X-Webhook-Secret header
  GHL_ROUTING_WEBHOOK   (optional) GHL inbound webhook for module/buffer delivery
  GHL_CRISIS_WEBHOOK    (optional) GHL inbound webhook for crisis alerts to Sandy
  GEMINI_MODEL          (optional) default: gemini-3.5-flash
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request
from google import genai
from google.genai import types

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cbd")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
AIRTABLE_BASE = "app15O2dXYrCeVKb4"  # CBD Core
AT_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE}"

# --- Table IDs -------------------------------------------------------------
T_CLIENTS = "tblfPZYJSaJM8DGBl"
T_LOGS = "tblEK3w2sFXV4g5jk"
T_ASSESS = "tbl6k1vxYhGfaUY2v"
T_TRANSITIONS = "tblXCKPuDcyyI8VqW"
T_CRISIS = "tblWvBNGc2KbWHPfK"

# --- Field IDs (stable even if fields are renamed in the UI) ---------------
F_CLIENT = {
    "name": "fldgVKBkyFHPLmzM4",
    "email": "fldcADVS5PVl1JInN",
    "ghl_id": "fldWJa0rUtVObBmM6",
    "week": "fldk7CBpyhGqxoulJ",
    "state": "fldKGipaUdKrvJmMP",
    "buffer_element": "fldnnlVkB9DWz5npX",
    "regulated_days": "fldu9kg56G1ey9UHv",
}
F_LOG = {
    "log_id": "fldxT2Sy14cWXCYg0",
    "date": "fldMb0LJjzSxiyZUo",
    "journal": "fld0ojhGtFFoOOAfu",
    "sleep": "fldavgVPqDyn5nLfP",
    "energy": "fldendQ0XjXWe0QcE",
    "anxiety": "fldGcmRYnPoJvNlXM",
    "physical": "fldwEWtwfZS2lNhlZ",
    "source": "fldgDusrwX61Ilvtq",
    "processed": "fld0uEPhSFzQf22ZO",
    "client": "fldxVV2U9dFLnjEDa",
}
F_ASSESS = {
    "assess_id": "fldDaQE4XozsFmxxv",
    "ns_state": "fldP8pBQ4AKCM6Q4r",
    "signatures": "fldjqHXwu00Lytl4O",
    "confidence": "fldXLp1gnarAKIgeN",
    "action": "fldx6GYj97KkFtBJb",
    "buffer_element": "fldOTTpdLLuRtFhBL",
    "crisis": "fldjxYzPGHx6u6KXz",
    "crisis_alert": "fldxZHaU5f2ovk5os",
    "reasoning": "fldFSdkyazP5d94GZ",
    "model": "fldtWL3VNkNlIPpcU",
    "latency": "fldmnHBc3uOWmhShp",
    "tokens_in": "fldaA4hNnSJ6I9L7o",
    "tokens_out": "fldBjvRESfXHWsuGh",
    "raw_json": "fld6ZMdXa5dAGfcpl",
    "assessed_at": "fld4EGzWloU79UJWA",
    "daily_log": "fldf3wnNQGmTVS0Bc",
    "client": "fldE4pqaIgxZ040pQ",
}
F_TRANS = {
    "trans_id": "fldASvr8qDKQtyLAw",
    "from_state": "fldq0cpsrYw98CxDw",
    "to_state": "fldJ8dxZctQU1tzOZ",
    "actor": "fldEyHcclIYQUDM6a",
    "timestamp": "fldUqOIUfVzJU0YxT",
    "client": "fldjKhpzoYijXmGy6",
    "assessment": "fldAJ4vFe0wAGP3Ss",
}
F_CRISIS = {
    "alert_id": "fldqMTPoljYuaV14s",
    "client": "fldoLJJkT1zHbpdlR",
    "client_name": "fldvxeEMl6tdEGojh",
    "client_email": "fldYzD6ZRZ1xwn8bB",
    "reasoning": "fldSMPsKySOUcDsBN",
    "assessment": "fldgq1Qptv0bwrrU0",
    "flagged_at": "fldngpL8kr8LxZMiW",
}

# Gemini enum -> Airtable select option names
NS_STATE_MAP = {
    "REGULATED": "Regulated",
    "SYMPATHETIC_ACTIVATION": "Sympathetic Activation",
    "FREEZE_SHUTDOWN": "Freeze-Shutdown",
    "PHYSICAL_FLARE": "Physical Flare",
}
ACTION_MAP = {
    "ADVANCE": "Advance",
    "HOLD": "Hold",
    "ROUTE_TO_BUFFER": "Route to Buffer",
}
ELEMENT_MAP = {"EARTH": "Earth", "AIR": "Air", "WATER": "Water",
               "FIRE": "Fire", "SPIRIT": "Spirit"}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "nervous_system_state": {"type": "STRING", "enum": list(NS_STATE_MAP)},
        "confidence": {"type": "NUMBER"},
        "stress_signatures": {
            "type": "ARRAY",
            "items": {"type": "STRING", "enum": [
                "anxiety_loop", "rumination", "sleep_disruption", "overwhelm",
                "dissociation", "pain_flare", "fatigue_crash", "digestive_flare",
                "emotional_surge", "avoidance", "self_criticism"]},
        },
        "recommended_action": {"type": "STRING", "enum": list(ACTION_MAP)},
        "buffer_element": {"type": "STRING",
                           "enum": list(ELEMENT_MAP), "nullable": True},
        "crisis_flag": {"type": "BOOLEAN"},
        "reasoning_summary": {"type": "STRING"},
    },
    "required": ["nervous_system_state", "confidence", "stress_signatures",
                 "recommended_action", "crisis_flag", "reasoning_summary"],
}

with open(os.path.join(os.path.dirname(__file__), "system_prompt.txt")) as f:
    SYSTEM_PROMPT = f.read()

REENTRY_THRESHOLD = 2  # consecutive Regulated days required to exit buffer
FINAL_WEEK = 10

# ---------------------------------------------------------------------------
# Airtable helpers
# ---------------------------------------------------------------------------
def at_headers():
    return {"Authorization": f"Bearer {os.environ['AIRTABLE_API_KEY']}",
            "Content-Type": "application/json"}


def at_create(table, fields):
    r = requests.post(f"{AT_URL}/{table}", headers=at_headers(),
                      json={"fields": fields, "typecast": True}, timeout=30)
    r.raise_for_status()
    return r.json()


def at_update(table, record_id, fields):
    r = requests.patch(f"{AT_URL}/{table}/{record_id}", headers=at_headers(),
                       json={"fields": fields, "typecast": True}, timeout=30)
    r.raise_for_status()
    return r.json()


def find_client(email=None, ghl_id=None):
    if ghl_id:
        formula = f"{{GHL Contact ID}}='{ghl_id}'"
    else:
        formula = f"LOWER({{Email}})='{(email or '').lower()}'"
    r = requests.get(f"{AT_URL}/{T_CLIENTS}", headers=at_headers(),
                     params={"filterByFormula": formula, "maxRecords": 1,
                             "returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    recs = r.json().get("records", [])
    return recs[0] if recs else None


def prior_assessments(client_record_id, n=3):
    formula = f"FIND('{client_record_id}', ARRAYJOIN({{Client}}))"
    r = requests.get(f"{AT_URL}/{T_ASSESS}", headers=at_headers(),
                     params={"filterByFormula": formula, "maxRecords": n,
                             "sort[0][field]": "Assessed At",
                             "sort[0][direction]": "desc"}, timeout=30)
    if not r.ok:
        return []
    out = []
    for rec in r.json().get("records", []):
        f = rec.get("fields", {})
        out.append({"ns_state": f.get("NS State"),
                    "action": f.get("Recommended Action"),
                    "confidence": f.get("Confidence")})
    return out


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def run_assessment(journal, scores, context):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    payload = json.dumps({"journal_text": journal, "scores": scores,
                          "context": context})
    t0 = time.time()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=payload,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )
    latency_ms = int((time.time() - t0) * 1000)
    usage = resp.usage_metadata
    return (json.loads(resp.text), latency_ms,
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
def next_state(current_state, current_week, regulated_days, assessment):
    """Returns (new_state, new_week, new_regulated_days, buffer_element)."""
    action = assessment["recommended_action"]
    ns = assessment["nervous_system_state"]
    element = assessment.get("buffer_element")

    if assessment.get("crisis_flag"):
        return current_state, current_week, 0, None  # no AI routing on crisis

    if current_state == "Safety Buffer":
        if ns == "REGULATED":
            regulated_days += 1
            if regulated_days >= REENTRY_THRESHOLD:
                return "On Track", current_week, regulated_days, None
            return "Safety Buffer", current_week, regulated_days, None
        return "Safety Buffer", current_week, 0, element

    # current_state == On Track (Paused/Completed are manual states)
    if action == "ROUTE_TO_BUFFER":
        return "Safety Buffer", current_week, 0, element
    if action == "ADVANCE":
        new_week = min(current_week + 1, FINAL_WEEK)
        if current_week >= FINAL_WEEK:
            return "Completed", FINAL_WEEK, regulated_days + 1, None
        return "On Track", new_week, regulated_days + 1, None
    return current_state, current_week, regulated_days, None  # HOLD


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "cbd-assess",
                    "model": GEMINI_MODEL})


@app.post("/assess")
def assess():
    # -- auth --
    if request.headers.get("X-Webhook-Secret") != os.environ["WEBHOOK_SECRET"]:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    # GHL's Webhook action nests custom key-value pairs under "customData"
    # instead of the request body's top level.
    data = body.get("customData") or body
    journal = (data.get("journal_text") or "").strip()
    scores = {k: data.get(k) for k in
              ("sleep", "energy", "anxiety", "physical_symptoms")}
    if not journal:
        return jsonify({"error": "journal_text required"}), 400

    # -- 1. find client --
    client_rec = find_client(email=data.get("email"),
                             ghl_id=data.get("ghl_contact_id"))
    if not client_rec:
        return jsonify({"error": "client not found"}), 404
    cf = client_rec["fields"]
    client_id = client_rec["id"]
    client_name = cf.get(F_CLIENT["name"], "Unknown")
    current_state = cf.get(F_CLIENT["state"], "On Track")
    current_week = cf.get(F_CLIENT["week"], 0) or 0
    regulated_days = cf.get(F_CLIENT["regulated_days"], 0) or 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # -- 2. write daily log --
    log_rec = at_create(T_LOGS, {
        F_LOG["log_id"]: f"{client_name}-{today}",
        F_LOG["date"]: today,
        F_LOG["journal"]: journal,
        F_LOG["sleep"]: scores["sleep"],
        F_LOG["energy"]: scores["energy"],
        F_LOG["anxiety"]: scores["anxiety"],
        F_LOG["physical"]: scores["physical_symptoms"],
        F_LOG["source"]: data.get("source", "GHL Form"),
        F_LOG["client"]: [client_id],
    })

    # -- 3. Gemini assessment --
    context = {"current_week": current_week, "current_state": current_state,
               "prior_assessments": prior_assessments(client_id)}
    result, latency_ms, tok_in, tok_out = run_assessment(journal, scores,
                                                         context)

    # -- 4. write assessment --
    assess_rec = at_create(T_ASSESS, {
        F_ASSESS["assess_id"]: f"{client_name}-{today}-A",
        F_ASSESS["ns_state"]: NS_STATE_MAP[result["nervous_system_state"]],
        F_ASSESS["signatures"]: result.get("stress_signatures", []),
        F_ASSESS["confidence"]: round(float(result["confidence"]), 2),
        F_ASSESS["action"]: ACTION_MAP[result["recommended_action"]],
        F_ASSESS["buffer_element"]: ELEMENT_MAP.get(
            (result.get("buffer_element") or "").upper()),
        F_ASSESS["crisis"]: bool(result.get("crisis_flag")),
        F_ASSESS["crisis_alert"]: "Yes" if result.get("crisis_flag") else "No",
        F_ASSESS["reasoning"]: result.get("reasoning_summary", ""),
        F_ASSESS["model"]: GEMINI_MODEL,
        F_ASSESS["latency"]: latency_ms,
        F_ASSESS["tokens_in"]: tok_in,
        F_ASSESS["tokens_out"]: tok_out,
        F_ASSESS["raw_json"]: json.dumps(result),
        F_ASSESS["assessed_at"]: datetime.now(timezone.utc).isoformat(),
        F_ASSESS["daily_log"]: [log_rec["id"]],
        F_ASSESS["client"]: [client_id],
    })
    at_update(T_LOGS, log_rec["id"], {F_LOG["processed"]: True})

    # -- telemetry (structured -> Cloud Logging) --
    log.info(json.dumps({
        "event": "assessment", "client": client_name,
        "ns_state": result["nervous_system_state"],
        "action": result["recommended_action"],
        "confidence": result["confidence"], "crisis": result.get("crisis_flag"),
        "latency_ms": latency_ms, "tokens_in": tok_in, "tokens_out": tok_out,
        "model": GEMINI_MODEL,
    }))

    # -- 5. crisis path: alert human, no AI routing --
    if result.get("crisis_flag"):
        at_create(T_CRISIS, {
            F_CRISIS["alert_id"]: f"{client_name}-{today}-CRISIS",
            F_CRISIS["client"]: [client_id],
            F_CRISIS["client_name"]: client_name,
            F_CRISIS["client_email"]: cf.get(F_CLIENT["email"]),
            F_CRISIS["reasoning"]: result.get("reasoning_summary", ""),
            F_CRISIS["assessment"]: [assess_rec["id"]],
            F_CRISIS["flagged_at"]: datetime.now(timezone.utc).isoformat(),
        })
        hook = os.environ.get("GHL_CRISIS_WEBHOOK")
        if hook:
            requests.post(hook, json={
                "type": "CRISIS_ALERT", "client": client_name,
                "email": cf.get(F_CLIENT["email"]),
                "assessment_id": assess_rec["id"]}, timeout=15)
        return jsonify({"status": "crisis_flagged",
                        "routing": "human_alert_sent"}), 200

    # -- 6. state machine --
    new_state, new_week, new_reg_days, element = next_state(
        current_state, current_week, regulated_days, result)

    update = {F_CLIENT["week"]: new_week,
              F_CLIENT["regulated_days"]: new_reg_days}
    if new_state != current_state:
        update[F_CLIENT["state"]] = new_state
        update[F_CLIENT["buffer_element"]] = (
            ELEMENT_MAP.get((element or "").upper())
            if new_state == "Safety Buffer" else None)
        at_create(T_TRANSITIONS, {
            F_TRANS["trans_id"]:
                f"{client_name}-{datetime.now(timezone.utc).isoformat()}",
            F_TRANS["from_state"]: current_state,
            F_TRANS["to_state"]: new_state,
            F_TRANS["actor"]: "AI",
            F_TRANS["timestamp"]: datetime.now(timezone.utc).isoformat(),
            F_TRANS["client"]: [client_id],
            F_TRANS["assessment"]: [assess_rec["id"]],
        })
    at_update(T_CLIENTS, client_id, update)

    # -- 7. tell GHL what to deliver --
    hook = os.environ.get("GHL_ROUTING_WEBHOOK")
    if hook:
        requests.post(hook, json={
            "type": "ROUTING", "client": client_name,
            "email": cf.get(F_CLIENT["email"]),
            "ghl_contact_id": cf.get(F_CLIENT["ghl_id"]),
            "state": new_state, "week": new_week,
            "buffer_element": ELEMENT_MAP.get((element or "").upper()),
            "action": result["recommended_action"]}, timeout=15)

    return jsonify({"status": "ok", "state": new_state, "week": new_week,
                    "action": result["recommended_action"],
                    "assessment_id": assess_rec["id"]}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
