"""
Calm by Design - Assessment Engine (Cloud Run)
================================================
Enrollment + daily check-in + assessment hub for the CBD adaptive
curriculum. Serves both the new participant-facing web pages and the
existing GHL-triggered webhook, through one shared assessment core so the
two paths can't drift apart.

Flow (new Flask path):
  /enroll -> Client created/updated (Airtable) -> access token issued
  /checkin -> process_checkin() -> Daily Log + AI Assessment (Airtable)
              -> Gemini structured assessment + deterministic safety rule
              -> support score/tier -> response route -> curriculum update
              -> instant on-screen result

Flow (existing GHL path, unchanged from the participant's perspective):
  GHL check-in form -> POST /assess -> same process_checkin() core
              -> outbound GHL webhook (optional) / GHL polls Airtable

Env vars required (set on Cloud Run):
  GEMINI_API_KEY        Google AI Studio key
  AIRTABLE_API_KEY      Airtable personal access token
  WEBHOOK_SECRET        shared secret; GHL must send X-Webhook-Secret header
  SESSION_SECRET        secret for CSRF token generation (never in source)
  GHL_ROUTING_WEBHOOK   (optional) GHL inbound webhook for module/buffer delivery
  GHL_CRISIS_WEBHOOK    (optional) GHL inbound webhook for crisis alerts to Sandy
  GEMINI_MODEL          (optional) default: gemini-3.5-flash

Non-clinical: the support score and AI classification are routing signals
only, never a diagnosis, medical assessment, or validated instrument. The
safety route is not monitored in real time and is not emergency care.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, make_response, redirect, render_template, request
from google import genai
from google.genai import types
from werkzeug.exceptions import HTTPException

from elements_content import ELEMENTS, SAFETY_FOOTER, next_element
from routing_config import (
    FINAL_WEEK,
    MIN_DAYS_BETWEEN_ADVANCES,
    REENTRY_THRESHOLD,
    ROUTE_GROUNDING_SUPPORT,
    ROUTE_HEIGHTENED_SUPPORT,
    ROUTE_LABELS,
    ROUTE_MEDICAL_EMERGENCY,
    ROUTE_POSITIVE_PROGRESS,
    ROUTE_SAFETY,
    ROUTE_STEADY,
    TIER_LABELS,
)
from routing_config import route as compute_route
from routing_config import score_tier, support_score
from safety_rules import check_medical_emergency, check_safety, is_safety_triggering

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cbd")

APP_VERSION = "2.0.0"

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
T_RECOVERY = "tbljg4niOIuRj30q2"

# --- Field IDs (stable even if fields are renamed in the UI) ---------------
F_CLIENT = {
    "name": "fldgVKBkyFHPLmzM4",
    "email": "fldcADVS5PVl1JInN",
    "phone": "fldWWqFGEW3y2ykHA",
    "ghl_id": "fldWJa0rUtVObBmM6",
    "week": "fldk7CBpyhGqxoulJ",
    "state": "fldKGipaUdKrvJmMP",
    "buffer_element": "fldnnlVkB9DWz5npX",
    "regulated_days": "fldu9kg56G1ey9UHv",
    "sms_consent": "fldkYsjpbgrp7K8nZ",
    "marketing_consent": "fldH65G8A2CLAIf68",
    "access_token_hash": "fldPKA6lM25CrsQER",
    "access_token_issued_at": "fldJEMrlPgXDFgCV8",
    "access_token_expires_at": "fldjjPixAzH2vXxmO",
    "week_started_at": "fldj6mCKRFXu42QDu",
    "test_record": "fld7u1DDBZfjfPWJc",
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
    "submission_id": "fldtvcyW8rVooOTmT",
    "ai_assessments": "flde47lBuRzUTNwJA",
}
F_ASSESS = {
    "assess_id": "fldDaQE4XozsFmxxv",
    "confidence": "fldXLp1gnarAKIgeN",
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
    "support_score": "fldve8OZBn09Rs5n0",
    "score_tier": "fldGpjrpOR95Yzrdg",
    "sentiment": "fldlVfJijFljdD3O8",
    "progress_signal": "fldl1NuxXHdij85Z2",
    "distress_signal": "fldDbBjIcXVzxnSFc",
    "safety_signal": "fldYachx5kD19MaLm",
    "trigger_reasons": "flddb6H0oGk5eSORz",
    "suggested_element": "fldA4J30TZLhuSpO1",
    "response_route": "fldhYfzozYbkyYKPd",
    "safety_trigger_source": "fld70F9gj53k1PE8O",
    "fallback_mode": "fldHTlmIOO2AupPNJ",
    "fallback_reason": "fldkVdvR00rP9ZCuQ",
    "ghl_action": "fldbxhdsnYg5EXdZ8",
    "owner_alert_status": "fldVX6YtrDwgrawQO",
    "app_version": "fldoreDswsDvsS8jC",
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
F_RECOVERY = {
    "request_id": "fldIyDEzMxJLBLvtH",
    "client": "flddjdNBwjqrT4WcD",
    "token_hash": "fldl5QSMqaBZmIEIF",
    "requested_at": "fldZE0Ukt3ZhMmjw7",
    "expires_at": "fldyXuVBe37wrubiL",
    "used_at": "fld06ovCHj66C7eOm",
    "recovery_link": "fldURcX1nJFSchVwd",
}

ELEMENT_ENUM_MAP = {"earth": "Earth", "air": "Air", "water": "Water", "fire": "Fire", "spirit": "Spirit"}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sentiment": {"type": "STRING", "enum": ["positive", "neutral", "distressed"]},
        "progress_signal": {"type": "BOOLEAN"},
        "distress_signal": {"type": "BOOLEAN"},
        "safety_signal": {"type": "STRING",
                          "enum": ["none", "ambiguous", "direct_self_harm", "imminent_danger"]},
        "medical_emergency_signal": {"type": "BOOLEAN"},
        "trigger_reasons": {"type": "ARRAY", "items": {"type": "STRING"}},
        "suggested_element": {"type": "STRING", "enum": list(ELEMENT_ENUM_MAP)},
        "confidence": {"type": "NUMBER"},
        "summary": {"type": "STRING"},
    },
    "required": ["sentiment", "progress_signal", "distress_signal", "safety_signal",
                 "medical_emergency_signal", "trigger_reasons", "suggested_element",
                 "confidence", "summary"],
}

with open(os.path.join(os.path.dirname(__file__), "system_prompt.txt")) as f:
    SYSTEM_PROMPT = f.read()

ACCESS_TOKEN_TTL_DAYS = 90
RECOVERY_TOKEN_TTL_MINUTES = 30
RECOVERY_RATE_LIMIT_PER_HOUR = 5
MAX_JOURNAL_LENGTH = 5000
IP_RATE_LIMIT = {
    "enroll": (10, 3600),
    "recover": (10, 3600),
    "checkin": (30, 3600),
    # GHL may submit multiple participants from one egress address. This is a
    # high enough operational ceiling to avoid normal batching while still
    # bounding a leaked webhook secret within one app instance.
    "assess": (300, 60),
}
SESSION_COOKIE = "cbd_token"
CSRF_COOKIE = "cbd_csrf"

# ---------------------------------------------------------------------------
# In-process rate limiter (single Cloud Run worker/instance today; resets on
# restart and doesn't share state across instances - a documented,
# smallest-correct tradeoff for this MVP's traffic level, not a production
# guarantee).
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_rate_buckets = defaultdict(deque)


def rate_limited(bucket, ip):
    limit, window = IP_RATE_LIMIT[bucket]
    key = f"{bucket}:{ip}"
    now = time.time()
    with _rate_lock:
        q = _rate_buckets[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


def client_ip():
    # Do not trust a caller-controlled X-Forwarded-For chain without an
    # explicitly verified ProxyFix hop count. Cloud Run proxy behavior must be
    # confirmed during controlled deployment testing before changing this.
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Airtable helpers
# ---------------------------------------------------------------------------
def _airtable_string_literal(value):
    """Escape a value embedded inside a single-quoted Airtable formula."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def new_submission_id():
    """Return the one accepted replay-key format: a canonical UUIDv4."""
    return str(uuid.uuid4())


def validate_submission_id(value):
    """Return a canonical UUIDv4 or raise without contacting Airtable."""
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid submission_id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid submission_id") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("invalid submission_id")
    return str(parsed)


AIRTABLE_RECORD_ID_RE = re.compile(r"^rec[A-Za-z0-9]{14}$")
MAX_REPLAY_CANDIDATES = 10


class ReplayLookupUnavailable(Exception):
    """A replay key exists, but no unique, safely owned record can be used."""


def _valid_airtable_record_id(value):
    return isinstance(value, str) and bool(AIRTABLE_RECORD_ID_RE.fullmatch(value))


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


def at_get(table, record_id):
    r = requests.get(f"{AT_URL}/{table}/{record_id}", headers=at_headers(),
                     params={"returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    return r.json()


def find_client(email=None, ghl_id=None):
    if ghl_id:
        formula = "{GHL Contact ID}='%s'" % _airtable_string_literal(ghl_id)
    else:
        formula = "LOWER({Email})='%s'" % _airtable_string_literal((email or "").lower())
    r = requests.get(f"{AT_URL}/{T_CLIENTS}", headers=at_headers(),
                     params={"filterByFormula": formula, "maxRecords": 1,
                             "returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    recs = r.json().get("records", [])
    return recs[0] if recs else None


def find_client_by_access_token(raw_token):
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    formula = "{Access Token Hash}='%s'" % _airtable_string_literal(token_hash)
    r = requests.get(f"{AT_URL}/{T_CLIENTS}", headers=at_headers(),
                     params={"filterByFormula": formula, "maxRecords": 1,
                             "returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    recs = r.json().get("records", [])
    if not recs:
        return None
    rec = recs[0]
    expires = rec["fields"].get(F_CLIENT["access_token_expires_at"])
    if not expires or _parse_iso(expires) < datetime.now(timezone.utc):
        return None
    return rec


def _single_client_link(record, field_id):
    """Return one valid Client record ID, or None for any malformed shape."""
    if not isinstance(record, dict):
        return None
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    links = fields.get(field_id)
    if not isinstance(links, list) or len(links) != 1:
        return None
    link = links[0]
    return link if _valid_airtable_record_id(link) else None


def _record_links_to_client(record, field_id, client_record_id):
    return (
        _valid_airtable_record_id(client_record_id)
        and _single_client_link(record, field_id) == client_record_id
    )


def _select_owned_log_candidate(records, submission_id, client_record_id, truncated=False):
    """Select one exact owned replay candidate or fail closed.

    Airtable REST responses expose raw linked-record IDs in the fields object,
    so ownership is enforced here rather than through linked-field formula
    display values.
    """
    submission_id = validate_submission_id(submission_id)
    if not _valid_airtable_record_id(client_record_id):
        raise ReplayLookupUnavailable()
    if truncated or not isinstance(records, list):
        raise ReplayLookupUnavailable()
    if not records:
        return None

    owned = []
    malformed = False
    for record in records:
        if not isinstance(record, dict) or not _valid_airtable_record_id(record.get("id")):
            malformed = True
            continue
        fields = record.get("fields")
        if not isinstance(fields, dict) or fields.get(F_LOG["submission_id"]) != submission_id:
            malformed = True
            continue
        link = _single_client_link(record, F_LOG["client"])
        if link is None:
            malformed = True
        elif link == client_record_id:
            owned.append(record)

    if malformed or len(owned) != 1:
        raise ReplayLookupUnavailable()
    return owned[0]


def find_log_by_submission_id(submission_id, client_record_id):
    submission_id = validate_submission_id(submission_id)
    escaped_submission = _airtable_string_literal(submission_id)
    formula = "{Submission ID}='%s'" % escaped_submission
    r = requests.get(f"{AT_URL}/{T_LOGS}", headers=at_headers(),
                     params={"filterByFormula": formula,
                             "maxRecords": MAX_REPLAY_CANDIDATES,
                             "pageSize": MAX_REPLAY_CANDIDATES,
                             "returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict):
        raise ReplayLookupUnavailable()
    return _select_owned_log_candidate(
        payload.get("records"), submission_id, client_record_id,
        truncated=bool(payload.get("offset")),
    )


def find_assessment_by_log_id(log_record_id):
    """Look up the AI Assessment for a Daily Log directly via the
    assessment's own Daily Log link field, rather than relying on the log's
    reverse-link field having been populated yet."""
    formula = "FIND('%s', ARRAYJOIN({Daily Log}))" % _airtable_string_literal(log_record_id)
    r = requests.get(f"{AT_URL}/{T_ASSESS}", headers=at_headers(),
                     params={"filterByFormula": formula, "maxRecords": 1,
                             "returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    recs = r.json().get("records", [])
    return recs[0] if recs else None


def prior_assessments(client_record_id, n=3):
    formula = "FIND('%s', ARRAYJOIN({Client}))" % _airtable_string_literal(client_record_id)
    r = requests.get(f"{AT_URL}/{T_ASSESS}", headers=at_headers(),
                     params={"filterByFormula": formula, "maxRecords": n,
                             "returnFieldsByFieldId": "true",
                             "sort[0][field]": "Assessed At",
                             "sort[0][direction]": "desc"}, timeout=30)
    if not r.ok:
        return []
    out = []
    for rec in r.json().get("records", []):
        f = rec.get("fields", {})
        out.append({"response_route": f.get(F_ASSESS["response_route"]),
                    "sentiment": f.get(F_ASSESS["sentiment"]),
                    "confidence": f.get(F_ASSESS["confidence"])})
    return out


def find_recovery_by_token_hash(token_hash):
    formula = "{Recovery Token Hash}='%s'" % _airtable_string_literal(token_hash)
    r = requests.get(f"{AT_URL}/{T_RECOVERY}", headers=at_headers(),
                     params={"filterByFormula": formula, "maxRecords": 1,
                             "returnFieldsByFieldId": "true"}, timeout=30)
    r.raise_for_status()
    recs = r.json().get("records", [])
    return recs[0] if recs else None


def count_recent_recovery_requests(email, hours=1):
    # Request ID is "{email}-{timestamp}"; scanning recent records by prefix
    # avoids needing a formula that echoes the raw email back in a filter.
    r = requests.get(f"{AT_URL}/{T_RECOVERY}", headers=at_headers(),
                     params={"maxRecords": 50, "returnFieldsByFieldId": "true",
                             "sort[0][field]": "Requested At",
                             "sort[0][direction]": "desc"}, timeout=30)
    if not r.ok:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    count = 0
    for rec in r.json().get("records", []):
        req_id = rec["fields"].get(F_RECOVERY["request_id"], "")
        requested_at = rec["fields"].get(F_RECOVERY["requested_at"])
        if not requested_at or _parse_iso(requested_at) < cutoff:
            continue
        if req_id.lower().startswith(email.lower() + "-"):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Tokens / session / CSRF
# ---------------------------------------------------------------------------
def _session_secret():
    return os.environ["SESSION_SECRET"]


def new_random_token():
    return secrets.token_urlsafe(32)


def access_fragment_path(raw_token):
    """Build a bearer link for separate trusted delivery, never HTML output."""
    if not valid_bearer_token(raw_token):
        raise ValueError("invalid access token")
    return f"/access#t={raw_token}"


def recovery_fragment_path(raw_token):
    if not valid_bearer_token(raw_token):
        raise ValueError("invalid recovery token")
    return f"/recover-access#rt={raw_token}"


def valid_bearer_token(raw_token):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", raw_token or ""))


def hash_token(raw_token):
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def issue_access_token(client_id):
    raw_token = new_random_token()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=ACCESS_TOKEN_TTL_DAYS)
    at_update(T_CLIENTS, client_id, {
        F_CLIENT["access_token_hash"]: hash_token(raw_token),
        F_CLIENT["access_token_issued_at"]: now.isoformat(),
        F_CLIENT["access_token_expires_at"]: expires.isoformat(),
    })
    return raw_token, expires


def set_access_cookie(resp, raw_token, expires):
    max_age = max(int((expires - datetime.now(timezone.utc)).total_seconds()), 60)
    resp.set_cookie(SESSION_COOKIE, raw_token, max_age=max_age, httponly=True,
                    secure=True, samesite="Lax")
    return resp


def get_current_client():
    raw_token = request.cookies.get(SESSION_COOKIE)
    return find_client_by_access_token(raw_token) if raw_token else None


def new_csrf_token():
    return hmac.new(_session_secret().encode(), secrets.token_bytes(18), hashlib.sha256).hexdigest()


def csrf_render(template, status=200, **kwargs):
    """Render a participant-facing form page with a fresh CSRF token embedded
    in the form AND set as a matching cookie on the actual returned response
    (double-submit pattern) - the cookie must be set on the response that's
    really sent, not a throwaway one."""
    token = new_csrf_token()
    resp = make_response(render_template(template, csrf_token=token, **kwargs), status)
    resp.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="Lax", max_age=3600)
    return resp


def valid_csrf():
    cookie_val = request.cookies.get(CSRF_COOKIE, "")
    form_val = request.form.get("csrf_token", "")
    return bool(cookie_val) and hmac.compare_digest(cookie_val, form_val)


def no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


REDEMPTION_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def protect_redemption_response(resp):
    """Prevent token-redemption documents and responses from being retained."""
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = REDEMPTION_CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _bearer_query_present():
    """Reject bearer-shaped query parameters without reading their values."""
    return "t" in request.args or "rt" in request.args


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


def validate_gemini_result(result):
    required = RESPONSE_SCHEMA["required"]
    for key in required:
        if key not in result:
            raise ValueError(f"missing field: {key}")
    if result["sentiment"] not in ("positive", "neutral", "distressed"):
        raise ValueError("invalid sentiment")
    if result["safety_signal"] not in ("none", "ambiguous", "direct_self_harm", "imminent_danger"):
        raise ValueError("invalid safety_signal")
    if result["suggested_element"] not in ELEMENT_ENUM_MAP:
        raise ValueError("invalid suggested_element")
    if not isinstance(result["progress_signal"], bool) or not isinstance(result["distress_signal"], bool):
        raise ValueError("progress_signal/distress_signal must be boolean")
    if not isinstance(result["medical_emergency_signal"], bool):
        raise ValueError("medical_emergency_signal must be boolean")
    if not isinstance(result["trigger_reasons"], list):
        raise ValueError("trigger_reasons must be a list")
    conf = float(result["confidence"])
    if not (0.0 <= conf <= 1.0):
        raise ValueError("confidence out of range")
    return True


# ---------------------------------------------------------------------------
# Curriculum state machine
# ---------------------------------------------------------------------------
def curriculum_action(response_route):
    if response_route in (ROUTE_SAFETY, ROUTE_MEDICAL_EMERGENCY):
        return "SAFETY"  # no curriculum movement on either safety override
    if response_route == ROUTE_POSITIVE_PROGRESS:
        return "ADVANCE"
    if response_route in (ROUTE_GROUNDING_SUPPORT, ROUTE_HEIGHTENED_SUPPORT):
        return "ROUTE_TO_BUFFER"
    return "HOLD"


def next_state(current_state, current_week, regulated_days, week_started_at, action, element):
    """Returns (new_state, new_week, new_regulated_days, new_week_started_at,
    buffer_element_for_update, state_changed)."""
    today = date.today()

    if action == "SAFETY":
        return current_state, current_week, 0, week_started_at, None, False

    if current_state == "Safety Buffer":
        if action != "ROUTE_TO_BUFFER":
            regulated_days += 1
            if regulated_days >= REENTRY_THRESHOLD:
                return "On Track", current_week, regulated_days, week_started_at, None, True
            return "Safety Buffer", current_week, regulated_days, week_started_at, None, False
        return "Safety Buffer", current_week, 0, week_started_at, element, False

    # current_state == On Track (Paused/Completed are manual states)
    if action == "ROUTE_TO_BUFFER":
        return "Safety Buffer", current_week, 0, week_started_at, element, True

    if action == "ADVANCE":
        gate_open = (not week_started_at) or (
            today - date.fromisoformat(week_started_at[:10])).days >= MIN_DAYS_BETWEEN_ADVANCES
        if not gate_open:
            return current_state, current_week, regulated_days, week_started_at, None, False
        new_week = min(current_week + 1, FINAL_WEEK)
        if current_week >= FINAL_WEEK:
            return "Completed", FINAL_WEEK, regulated_days + 1, week_started_at, None, True
        return "On Track", new_week, regulated_days + 1, today.isoformat(), None, True

    return current_state, current_week, regulated_days, week_started_at, None, False  # HOLD


# ---------------------------------------------------------------------------
# Shared assessment core - used by both the new Flask check-in page and the
# existing GHL-triggered /assess webhook, so the two paths can't drift apart.
# ---------------------------------------------------------------------------
def process_checkin(client_rec, physical, anxiety, energy, sleep, journal, submission_id, source):
    client_id = client_rec["id"]
    cf = client_rec["fields"]
    client_name = cf.get(F_CLIENT["name"], "Unknown")
    submission_id = (
        validate_submission_id(submission_id) if submission_id else new_submission_id())

    # Run both deterministic backstops before any Airtable operation in this
    # shared core. If persistence is unavailable, clear current emergency
    # language must still produce the participant-facing 988/911 resources.
    keyword_signal, _keyword_phrase = check_safety(journal)
    keyword_medical, keyword_medical_phrase = check_medical_emergency(journal)
    keyword_safety_triggered = is_safety_triggering(keyword_signal)

    try:
        existing_log = find_log_by_submission_id(submission_id, client_id)
    except Exception as exc:
        if keyword_safety_triggered or keyword_medical:
            return _build_early_emergency_result(
                None, physical, anxiety, energy, sleep,
                keyword_safety_triggered, keyword_medical,
                checkin_saved=None, crisis_alert_created=None,
                failure_event="checkin_replay_log_lookup_failed",
                error_type=type(exc).__name__)
        raise
    # Independently enforce the returned relationship so a malformed or stale
    # lookup implementation cannot cross participant boundaries or fall
    # through to a duplicate create.
    if existing_log and not _record_links_to_client(
            existing_log, F_LOG["client"], client_id):
        if keyword_safety_triggered or keyword_medical:
            return _build_early_emergency_result(
                None, physical, anxiety, energy, sleep,
                keyword_safety_triggered, keyword_medical,
                checkin_saved=None, crisis_alert_created=None,
                failure_event="checkin_replay_log_ownership_failed",
                error_type="ReplayLookupUnavailable")
        raise ReplayLookupUnavailable()
    if existing_log:
        try:
            existing_assessment = find_assessment_by_log_id(existing_log["id"])
        except Exception as exc:
            if keyword_safety_triggered or keyword_medical:
                return _build_early_emergency_result(
                    None, physical, anxiety, energy, sleep,
                    keyword_safety_triggered, keyword_medical,
                    checkin_saved=True, crisis_alert_created=None,
                    failure_event="checkin_replay_assessment_lookup_failed",
                    error_type=type(exc).__name__)
            raise
        if existing_assessment and _record_links_to_client(
                existing_assessment, F_ASSESS["client"], client_id):
            return _result_from_assessment(existing_assessment)
        # A saved log without a provably owned assessment is a partial prior
        # submission. Never fall through to duplicate writes or webhooks.
        return _build_incomplete_replay_result(
            existing_log, physical, anxiety, energy, sleep, journal,
            keyword_safety_triggered, keyword_medical)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    score = support_score(physical, anxiety, energy, sleep)
    tier = score_tier(score)
    try:
        log_rec = at_create(T_LOGS, {
            F_LOG["log_id"]: f"{client_name}-{today}-{submission_id[:8]}",
            F_LOG["date"]: today,
            F_LOG["journal"]: journal,
            F_LOG["sleep"]: sleep,
            F_LOG["energy"]: energy,
            F_LOG["anxiety"]: anxiety,
            F_LOG["physical"]: physical,
            F_LOG["source"]: source,
            F_LOG["client"]: [client_id],
            F_LOG["submission_id"]: submission_id,
        })
    except Exception as exc:
        if keyword_safety_triggered or keyword_medical:
            return _build_early_emergency_result(
                client_rec, physical, anxiety, energy, sleep,
                keyword_safety_triggered, keyword_medical,
                checkin_saved=False, failure_event="daily_log_create_failed",
                error_type=type(exc).__name__, attempt_crisis_alert=True)
        raise

    fallback_mode = False
    fallback_reason = ""
    gemini_result = None
    latency_ms = tok_in = tok_out = 0
    try:
        context = {"current_week": cf.get(F_CLIENT["week"], 0) or 0,
                   "current_state": cf.get(F_CLIENT["state"], "On Track"),
                   "prior_assessments": prior_assessments(client_id)}
        gemini_result, latency_ms, tok_in, tok_out = run_assessment(
            journal, {"physical_symptoms": physical, "anxiety": anxiety,
                      "energy": energy, "sleep": sleep}, context)
        validate_gemini_result(gemini_result)
    except Exception as exc:  # Gemini unavailable, timed out, or malformed output
        fallback_mode = True
        fallback_reason = type(exc).__name__
        log.info(json.dumps({"event": "gemini_fallback", "reason": type(exc).__name__}))
        gemini_result = None

    if gemini_result:
        sentiment = gemini_result["sentiment"]
        progress_signal = bool(gemini_result["progress_signal"])
        distress_signal = bool(gemini_result["distress_signal"])
        gemini_safety = gemini_result["safety_signal"]
        gemini_medical = bool(gemini_result["medical_emergency_signal"])
        trigger_reasons = gemini_result.get("trigger_reasons", [])
        suggested_raw = ELEMENT_ENUM_MAP.get((gemini_result.get("suggested_element") or "").lower())
        confidence = round(float(gemini_result.get("confidence", 0)), 2)
        summary = gemini_result.get("summary", "")
    else:
        sentiment = "distressed" if tier in ("GROUNDING_SUPPORT", "HEIGHTENED_SUPPORT") else "neutral"
        progress_signal = False
        distress_signal = tier in ("GROUNDING_SUPPORT", "HEIGHTENED_SUPPORT")
        gemini_safety = "none"
        gemini_medical = False
        trigger_reasons = ["fallback: deterministic score-based routing used"]
        suggested_raw = None
        confidence = 0.0
        summary = "Gemini was unavailable or returned an invalid response; deterministic fallback used."

    safety_triggered = is_safety_triggering(gemini_safety) or keyword_safety_triggered
    if is_safety_triggering(gemini_safety) and is_safety_triggering(keyword_signal):
        safety_source = "both"
    elif is_safety_triggering(gemini_safety):
        safety_source = "gemini"
    elif is_safety_triggering(keyword_signal):
        safety_source = "keyword_rule"
    else:
        safety_source = "none"

    # Medical emergency: independent of safety_triggered above - a journal
    # entry can trip either, both, or neither. Never inferred from the other.
    medical_triggered = gemini_medical or keyword_medical
    if gemini_medical and keyword_medical:
        medical_source = "both"
    elif gemini_medical:
        medical_source = "gemini"
    elif keyword_medical:
        medical_source = "keyword_rule"
    else:
        medical_source = "none"
    if medical_triggered:
        trigger_reasons = list(trigger_reasons) + [
            "medical_emergency_signal: true (source=%s%s)" % (
                medical_source,
                f", phrase='{keyword_medical_phrase}'" if keyword_medical_phrase else "")
        ]

    # Medical-emergency override is a separate safety override from the five
    # wellness routes/ROUTE_SAFETY - it is checked here, before route(), and
    # route() itself is unchanged. If self-harm language is ALSO present,
    # ROUTE_SAFETY still wins (medical_triggered is still recorded above and
    # rendered alongside it - see result.html) so the crisis/owner-alert path
    # below still fires and 988 still shows, per spec.
    if medical_triggered and not safety_triggered:
        response_route = ROUTE_MEDICAL_EMERGENCY
    else:
        response_route = compute_route(tier, safety_triggered, distress_signal, progress_signal)

    last_element = cf.get(F_CLIENT["buffer_element"])
    element = None
    if response_route in (ROUTE_GROUNDING_SUPPORT, ROUTE_HEIGHTENED_SUPPORT):
        element = next_element(suggested_raw, last_element)

    assess_id = f"{client_name}-{today}-{submission_id[:8]}-A"
    assess_fields = {
        F_ASSESS["assess_id"]: assess_id,
        F_ASSESS["confidence"]: confidence,
        F_ASSESS["reasoning"]: (summary or "")[:500],
        F_ASSESS["model"]: GEMINI_MODEL if gemini_result else "fallback",
        F_ASSESS["latency"]: latency_ms,
        F_ASSESS["tokens_in"]: tok_in,
        F_ASSESS["tokens_out"]: tok_out,
        F_ASSESS["raw_json"]: json.dumps(gemini_result) if gemini_result else "{}",
        F_ASSESS["assessed_at"]: datetime.now(timezone.utc).isoformat(),
        F_ASSESS["daily_log"]: [log_rec["id"]],
        F_ASSESS["client"]: [client_id],
        F_ASSESS["support_score"]: score,
        F_ASSESS["score_tier"]: TIER_LABELS[tier],
        F_ASSESS["sentiment"]: sentiment,
        F_ASSESS["progress_signal"]: progress_signal,
        F_ASSESS["distress_signal"]: distress_signal,
        F_ASSESS["safety_signal"]: gemini_safety,
        F_ASSESS["trigger_reasons"]: json.dumps(trigger_reasons),
        F_ASSESS["suggested_element"]: suggested_raw,
        F_ASSESS["response_route"]: ROUTE_LABELS[response_route],
        F_ASSESS["safety_trigger_source"]: safety_source,
        F_ASSESS["fallback_mode"]: fallback_mode,
        F_ASSESS["fallback_reason"]: fallback_reason,
        F_ASSESS["crisis"]: response_route in (ROUTE_SAFETY, ROUTE_MEDICAL_EMERGENCY),
        F_ASSESS["crisis_alert"]: (
            "Yes" if response_route in (ROUTE_SAFETY, ROUTE_MEDICAL_EMERGENCY) else "No"),
        F_ASSESS["buffer_element"]: element,
        F_ASSESS["app_version"]: APP_VERSION,
        F_ASSESS["ghl_action"]: "",
    }

    # -- safety/medical-emergency overrides: alert human, no curriculum
    # movement. Both self-harm and medical-emergency signals are tracked
    # independently above; when both are present response_route is
    # ROUTE_SAFETY (self-harm takes precedence for routing/curriculum) but
    # the alert category and the on-screen result both still reflect medical
    # involvement - see _build_result's medical_emergency_triggered param
    # and result.html.
    if response_route in (ROUTE_SAFETY, ROUTE_MEDICAL_EMERGENCY):
        if response_route == ROUTE_MEDICAL_EMERGENCY:
            alert_category = "MEDICAL_EMERGENCY"
        elif medical_triggered:
            alert_category = "SELF_HARM_AND_MEDICAL_EMERGENCY"
        else:
            alert_category = "SELF_HARM"

        assess_fields[F_ASSESS["crisis"]] = True
        crisis_rec = _try_create_crisis_alert(client_rec, alert_category, summary)
        crisis_alert_created = crisis_rec is not None
        assess_fields[F_ASSESS["crisis_alert"]] = "Yes" if crisis_alert_created else "No"

        if crisis_alert_created:
            hook = os.environ.get("GHL_CRISIS_WEBHOOK")
            if hook and cf.get(F_CLIENT["sms_consent"]):
                try:
                    hook_response = requests.post(
                        hook,
                        json={"type": alert_category, "client": client_name,
                              "email": cf.get(F_CLIENT["email"])},
                        timeout=15,
                    )
                    hook_response.raise_for_status()
                    # This records only that the webhook endpoint accepted the
                    # request. It is never treated as proof of email/SMS delivery.
                    assess_fields[F_ASSESS["ghl_action"]] = "crisis_webhook_sent"
                except Exception as exc:
                    _log_persistence_failure("crisis_webhook_failed", type(exc).__name__)

        assess_rec = None
        later_processing_failed = False
        try:
            assess_rec = at_create(T_ASSESS, assess_fields)
        except Exception as exc:
            later_processing_failed = True
            _log_persistence_failure("ai_assessment_create_failed", type(exc).__name__)
        if assess_rec:
            if crisis_rec:
                try:
                    at_update(T_CRISIS, crisis_rec["id"], {
                        F_CRISIS["assessment"]: [assess_rec["id"]]})
                except Exception as exc:
                    later_processing_failed = True
                    _log_persistence_failure("crisis_alert_link_failed", type(exc).__name__)
            try:
                at_update(T_LOGS, log_rec["id"], {F_LOG["processed"]: True})
            except Exception as exc:
                later_processing_failed = True
                _log_persistence_failure("daily_log_processed_update_failed", type(exc).__name__)
        return _build_result(response_route, score, tier, element, summary, trigger_reasons,
                             fallback_mode, medical_emergency_triggered=medical_triggered,
                             checkin_saved=True,
                             crisis_alert_created=crisis_alert_created,
                             later_processing_failed=later_processing_failed)

    # -- curriculum update --
    action = curriculum_action(response_route)
    current_state = cf.get(F_CLIENT["state"], "On Track")
    current_week = cf.get(F_CLIENT["week"], 0) or 0
    regulated_days = cf.get(F_CLIENT["regulated_days"], 0) or 0
    week_started_at = cf.get(F_CLIENT["week_started_at"])

    new_state, new_week, new_reg_days, new_week_started_at, buffer_el, changed = next_state(
        current_state, current_week, regulated_days, week_started_at, action, element)

    client_update = {F_CLIENT["week"]: new_week, F_CLIENT["regulated_days"]: new_reg_days}
    if new_week_started_at != week_started_at:
        client_update[F_CLIENT["week_started_at"]] = new_week_started_at
    if changed:
        client_update[F_CLIENT["state"]] = new_state
        client_update[F_CLIENT["buffer_element"]] = buffer_el
        at_create(T_TRANSITIONS, {
            F_TRANS["trans_id"]: f"{client_name}-{datetime.now(timezone.utc).isoformat()}",
            F_TRANS["from_state"]: current_state,
            F_TRANS["to_state"]: new_state,
            F_TRANS["actor"]: "AI",
            F_TRANS["timestamp"]: datetime.now(timezone.utc).isoformat(),
            F_TRANS["client"]: [client_id],
        })
    elif element:
        client_update[F_CLIENT["buffer_element"]] = element
    at_update(T_CLIENTS, client_id, client_update)

    hook = os.environ.get("GHL_ROUTING_WEBHOOK")
    ghl_action = ""
    if hook:
        try:
            requests.post(hook, json={"type": "ROUTING", "client": client_name,
                                      "email": cf.get(F_CLIENT["email"]),
                                      "response_route": ROUTE_LABELS[response_route],
                                      "element": element}, timeout=15)
            ghl_action = "routing_webhook_sent"
        except Exception:
            ghl_action = "routing_webhook_failed"
    assess_fields[F_ASSESS["ghl_action"]] = ghl_action
    assess_fields[F_ASSESS["owner_alert_status"]] = "not_applicable"

    at_create(T_ASSESS, assess_fields)
    at_update(T_LOGS, log_rec["id"], {F_LOG["processed"]: True})

    log.info(json.dumps({"event": "assessment",
                         "score_tier": tier, "response_route": response_route,
                         "fallback_mode": fallback_mode, "latency_ms": latency_ms,
                         "tokens_in": tok_in, "tokens_out": tok_out, "model": GEMINI_MODEL}))

    return _build_result(response_route, score, tier, element, summary, trigger_reasons,
                         fallback_mode, medical_emergency_triggered=medical_triggered)


def _build_result(response_route, score, tier, element, summary, trigger_reasons, fallback_mode,
                  medical_emergency_triggered=False, checkin_saved=True,
                  crisis_alert_created=False, owner_notification_confirmed=False,
                  http_status=200, later_processing_failed=False,
                  processing_incomplete=False):
    return {
        "response_route": response_route,
        "response_route_label": ROUTE_LABELS.get(response_route, ""),
        "score": score,
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "element": ELEMENTS.get(element) if element else None,
        "element_name": element,
        "summary": summary,
        "trigger_reasons": trigger_reasons,
        "fallback_mode": fallback_mode,
        "medical_emergency_triggered": medical_emergency_triggered,
        "checkin_saved": checkin_saved,
        "crisis_alert_created": crisis_alert_created,
        "owner_notification_confirmed": owner_notification_confirmed,
        "http_status": http_status,
        "later_processing_failed": later_processing_failed,
        "processing_incomplete": processing_incomplete,
        "safety_footer": SAFETY_FOOTER,
    }


def _saved_rating(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if int(value) != value or not 1 <= int(value) <= 10:
        return None
    return int(value)


def _build_incomplete_replay_result(existing_log, physical, anxiety, energy, sleep,
                                    journal, safety_triggered, medical_triggered):
    """Represent an owned saved log whose assessment is not provable.

    No persistence, Gemini, alert, or webhook operation is retried here.
    """
    log.info(json.dumps({"event": "replay_processing_incomplete"}))
    fields = existing_log.get("fields") if isinstance(existing_log, dict) else None
    fields = fields if isinstance(fields, dict) else {}
    saved_journal = fields.get(F_LOG["journal"])
    saved_journal = saved_journal if isinstance(saved_journal, str) else ""
    saved_safety_signal, _ = check_safety(saved_journal)
    saved_medical, _ = check_medical_emergency(saved_journal)
    safety_triggered = safety_triggered or is_safety_triggering(saved_safety_signal)
    medical_triggered = medical_triggered or saved_medical

    saved_ratings = (
        _saved_rating(fields.get(F_LOG["physical"])),
        _saved_rating(fields.get(F_LOG["anxiety"])),
        _saved_rating(fields.get(F_LOG["energy"])),
        _saved_rating(fields.get(F_LOG["sleep"])),
    )
    ratings = saved_ratings if all(v is not None for v in saved_ratings) else (
        physical, anxiety, energy, sleep)
    score = support_score(*ratings)
    tier = score_tier(score)
    if safety_triggered:
        response_route = ROUTE_SAFETY
    elif medical_triggered:
        response_route = ROUTE_MEDICAL_EMERGENCY
    else:
        response_route = None
    return _build_result(
        response_route, score, tier, None, "",
        ["saved check-in assessment unavailable during replay"],
        fallback_mode=False,
        medical_emergency_triggered=medical_triggered,
        checkin_saved=True,
        crisis_alert_created=None,
        owner_notification_confirmed=False,
        http_status=200,
        later_processing_failed=True,
        processing_incomplete=True,
    )


def _log_persistence_failure(event, error_type):
    """Log only operational state, never participant identity or content."""
    log.info(json.dumps({"event": event, "error_type": error_type}))


def _try_create_crisis_alert(client_rec, alert_category, summary):
    """Best-effort Crisis Alert creation.

    A returned record proves only that Airtable accepted the alert record. It
    does not prove that GHL delivered a notification or that a person saw it.
    """
    cf = client_rec["fields"]
    client_name = cf.get(F_CLIENT["name"], "Unknown")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        return at_create(T_CRISIS, {
            F_CRISIS["alert_id"]: f"{client_name}-{today}-{alert_category}",
            F_CRISIS["client"]: [client_rec["id"]],
            F_CRISIS["client_name"]: client_name,
            F_CRISIS["client_email"]: cf.get(F_CLIENT["email"]),
            F_CRISIS["reasoning"]: f"[{alert_category}] {(summary or '')[:480]}",
            F_CRISIS["flagged_at"]: datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        _log_persistence_failure("crisis_alert_create_failed", type(exc).__name__)
        return None


def _build_early_emergency_result(client_rec, physical, anxiety, energy, sleep,
                                  safety_triggered, medical_triggered,
                                  checkin_saved, failure_event, error_type,
                                  crisis_alert_created=False,
                                  attempt_crisis_alert=False,
                                  http_status=503):
    """Build deterministic emergency guidance when Airtable blocks the
    normal assessment flow. Gemini is deliberately not called here."""
    _log_persistence_failure(failure_event, error_type)
    score = support_score(physical, anxiety, energy, sleep)
    tier = score_tier(score)
    response_route = (
        ROUTE_SAFETY if safety_triggered else ROUTE_MEDICAL_EMERGENCY)
    if safety_triggered and medical_triggered:
        alert_category = "SELF_HARM_AND_MEDICAL_EMERGENCY"
    elif safety_triggered:
        alert_category = "SELF_HARM"
    else:
        alert_category = "MEDICAL_EMERGENCY"

    if attempt_crisis_alert and client_rec is not None:
        crisis_alert_created = _try_create_crisis_alert(
            client_rec, alert_category,
            "Deterministic emergency backstop triggered while check-in persistence was unavailable.") is not None

    return _build_result(
        response_route, score, tier, None, "",
        ["deterministic emergency backstop used during persistence outage"],
        fallback_mode=False,
        medical_emergency_triggered=medical_triggered,
        checkin_saved=checkin_saved,
        crisis_alert_created=crisis_alert_created,
        owner_notification_confirmed=False,
        http_status=http_status,
    )


def _result_from_assessment(assess_record):
    f = assess_record["fields"]
    route_label = f.get(F_ASSESS["response_route"], "Steady")
    route_key = {v: k for k, v in ROUTE_LABELS.items()}.get(route_label, ROUTE_STEADY)
    tier_label = f.get(F_ASSESS["score_tier"], "Steady")
    tier_key = {v: k for k, v in TIER_LABELS.items()}.get(tier_label, "STEADY")
    element = f.get(F_ASSESS["buffer_element"])
    reasons = []
    try:
        reasons = json.loads(f.get(F_ASSESS["trigger_reasons"], "[]"))
    except (TypeError, ValueError):
        pass
    # No dedicated Airtable field for this yet (see HANDOFF.md) - reconstruct
    # from the trigger_reasons marker written in process_checkin so a
    # duplicate-submission replay still renders the medical banner correctly.
    medical_emergency_triggered = any(
        isinstance(r, str) and r.startswith("medical_emergency_signal: true") for r in reasons)
    owner_status = f.get(F_ASSESS["owner_alert_status"])
    if owner_status == "sent":
        crisis_alert_created = True  # legacy records: set only after alert creation
    elif owner_status == "failed":
        # Historical "failed" could mean either Crisis Alert creation failed
        # or a later optional webhook failed after the record was created.
        crisis_alert_created = None
    else:
        crisis_alert_created = f.get(F_ASSESS["crisis_alert"]) == "Yes"
    return _build_result(route_key, f.get(F_ASSESS["support_score"], 0), tier_key, element,
                         f.get(F_ASSESS["reasoning"], ""), reasons,
                         bool(f.get(F_ASSESS["fallback_mode"])),
                         medical_emergency_triggered=medical_emergency_triggered,
                         checkin_saved=True,
                         crisis_alert_created=crisis_alert_created)


# ---------------------------------------------------------------------------
# Routes - health + existing GHL webhook
# ---------------------------------------------------------------------------
@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "cbd-assess", "version": APP_VERSION,
                    "model": GEMINI_MODEL})


@app.post("/assess")
def assess():
    if request.headers.get("X-Webhook-Secret") != os.environ["WEBHOOK_SECRET"]:
        return jsonify({"error": "unauthorized"}), 401
    if rate_limited("assess", client_ip()):
        return jsonify({"error": "too many requests"}), 429

    body = request.get_json(force=True, silent=True) or {}
    data = body.get("customData") or body
    journal = (data.get("journal_text") or "").strip()
    if not journal:
        return jsonify({"error": "journal_text required"}), 400
    if len(journal) > MAX_JOURNAL_LENGTH:
        return jsonify({"error": "journal_text too long"}), 400

    try:
        scores = {k: int(data.get(k)) for k in ("sleep", "energy", "anxiety", "physical_symptoms")}
        if not all(1 <= v <= 10 for v in scores.values()):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "sleep, energy, anxiety, physical_symptoms must be integers 1-10"}), 400

    try:
        submission_id = validate_submission_id(
            data.get("submission_id") or new_submission_id())
    except ValueError:
        return jsonify({"error": "invalid submission_id"}), 400

    client_rec = find_client(email=data.get("email"), ghl_id=data.get("ghl_contact_id"))
    if not client_rec:
        return jsonify({"error": "client not found"}), 404

    result = process_checkin(client_rec, scores["physical_symptoms"], scores["anxiety"],
                             scores["energy"], scores["sleep"], journal,
                             submission_id,
                             data.get("source", "GHL Form"))
    if result.get("http_status", 200) != 200 or result.get("later_processing_failed", False):
        # Preserve the established /assess failure contract. Participant-facing
        # emergency rendering and endpoint-specific status handling belong to /checkin.
        return jsonify({"error": "internal error"}), 500
    internal_keys = {
        "element", "checkin_saved", "crisis_alert_created",
        "owner_notification_confirmed", "http_status", "later_processing_failed",
        "processing_incomplete",
    }
    return jsonify({"status": "ok",
                    **{k: v for k, v in result.items() if k not in internal_keys}}), 200


# ---------------------------------------------------------------------------
# Routes - enrollment
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.get("/enroll")
def enroll_form():
    return no_store(csrf_render("enroll.html", error=None))


@app.post("/enroll")
def enroll_submit():
    if rate_limited("enroll", client_ip()):
        return no_store(csrf_render(
            "enroll.html", status=429, error="Too many attempts. Please try again in a bit."))

    if not valid_csrf():
        return no_store(csrf_render(
            "enroll.html", status=400, error="Your session expired. Please try again."))

    first = (request.form.get("first_name") or "").strip()
    last = (request.form.get("last_name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    sms_consent = request.form.get("sms_consent") == "on"
    marketing_consent = request.form.get("marketing_consent") == "on"

    if not (first and last and email and phone and EMAIL_RE.match(email)):
        return no_store(csrf_render(
            "enroll.html", error="Please fill in first name, last name, a valid email, and phone."))

    existing = find_client(email=email)
    if existing:
        create_recovery_request(email, client_rec=existing)
        return no_store(csrf_render("recover.html", message=GENERIC_RECOVERY_MESSAGE))

    fields = {
        F_CLIENT["name"]: f"{first} {last}",
        F_CLIENT["email"]: email,
        F_CLIENT["phone"]: phone,
        F_CLIENT["sms_consent"]: sms_consent,
        F_CLIENT["marketing_consent"]: marketing_consent,
    }
    fields[F_CLIENT["week"]] = 0
    fields[F_CLIENT["state"]] = "On Track"
    fields[F_CLIENT["regulated_days"]] = 0
    created = at_create(T_CLIENTS, fields)
    client_id = created["id"]

    raw_token, expires = issue_access_token(client_id)
    resp = make_response(redirect("/checkin"))
    resp = set_access_cookie(resp, raw_token, expires)
    resp.headers["Referrer-Policy"] = "no-referrer"
    return no_store(resp)


# ---------------------------------------------------------------------------
# Routes - check-in
# ---------------------------------------------------------------------------
@app.get("/access")
def access_redeem_page():
    if _bearer_query_present():
        return protect_redemption_response(make_response("", 400))
    return protect_redemption_response(make_response(render_template("redeem.html")))


@app.post("/access")
def access_redeem_submit():
    if _bearer_query_present():
        return protect_redemption_response(make_response("", 400))
    raw_token = request.form.get("token", "")
    if not valid_bearer_token(raw_token):
        return protect_redemption_response(make_response("", 400))
    client_rec = find_client_by_access_token(raw_token)
    if not client_rec:
        return protect_redemption_response(make_response("", 400))
    expires = _parse_iso(client_rec["fields"][F_CLIENT["access_token_expires_at"]])
    resp = make_response("", 204)
    resp = set_access_cookie(resp, raw_token, expires)
    return protect_redemption_response(resp)


@app.get("/checkin/verify")
def legacy_checkin_verify_rejected():
    """Bearer query parameters are intentionally no longer accepted."""
    return no_store(make_response(render_template("link_invalid.html"), 400))


@app.get("/link-invalid")
def link_invalid():
    return no_store(make_response(render_template("link_invalid.html"), 400))


@app.get("/checkin")
def checkin_form():
    client_rec = get_current_client()
    if not client_rec:
        return no_store(redirect("/recover"))
    submission_id = new_submission_id()
    name = client_rec["fields"].get(F_CLIENT["name"], "there")
    return no_store(csrf_render("checkin.html", name=name,
                                submission_id=submission_id, error=None))


@app.post("/checkin")
def checkin_submit():
    if not valid_csrf():
        return no_store(make_response(render_template("link_invalid.html"), 400))

    raw_submission_id = request.form.get("submission_id", "")
    try:
        submission_id = validate_submission_id(raw_submission_id)
    except ValueError:
        return no_store(csrf_render(
            "checkin.html", status=400, name="there",
            submission_id=new_submission_id(),
            error="This check-in form expired. Please try again."))
    journal = (request.form.get("journal") or "").strip()

    try:
        physical = int(request.form.get("physical_symptoms", ""))
        anxiety = int(request.form.get("anxiety", ""))
        energy = int(request.form.get("energy", ""))
        sleep = int(request.form.get("sleep", ""))
        if not all(1 <= v <= 10 for v in (physical, anxiety, energy, sleep)):
            raise ValueError
        if not journal or len(journal) > MAX_JOURNAL_LENGTH:
            raise ValueError
    except ValueError:
        return no_store(csrf_render(
            "checkin.html", name="there", submission_id=submission_id,
            error=(
                "Please enter a whole number from 1 to 10 for each rating, and a journal entry "
                f"of no more than {MAX_JOURNAL_LENGTH} characters.")))

    if rate_limited("checkin", client_ip()):
        keyword_signal, _ = check_safety(journal)
        keyword_medical, _ = check_medical_emergency(journal)
        keyword_safety_triggered = is_safety_triggering(keyword_signal)
        if keyword_safety_triggered or keyword_medical:
            result = _build_early_emergency_result(
                None, physical, anxiety, energy, sleep,
                keyword_safety_triggered, keyword_medical,
                checkin_saved=False,
                failure_event="checkin_rate_limited",
                error_type="RateLimited",
                crisis_alert_created=False,
                http_status=429,
            )
            http_status = result.pop("http_status", 429)
            result.pop("later_processing_failed", None)
            return no_store(make_response(
                render_template("result.html", **result), http_status))
        return no_store(csrf_render(
            "checkin.html", status=429, name="there",
            submission_id=new_submission_id(),
            error="Too many check-ins were submitted. Please wait a bit and try again."))

    # Identity is deliberately resolved only after CSRF, replay-key, score,
    # and journal-length validation, so malformed or oversized submissions do
    # not reach Airtable.
    identity_error = None
    try:
        client_rec = get_current_client()
    except Exception as exc:
        client_rec = None
        identity_error = exc

    if identity_error is None and not client_rec:
        return no_store(redirect("/recover"))

    if identity_error is not None:
        keyword_signal, _ = check_safety(journal)
        keyword_medical, _ = check_medical_emergency(journal)
        keyword_safety_triggered = is_safety_triggering(keyword_signal)
        if keyword_safety_triggered or keyword_medical:
            result = _build_early_emergency_result(
                None, physical, anxiety, energy, sleep,
                keyword_safety_triggered, keyword_medical,
                checkin_saved=False,
                failure_event="checkin_identity_lookup_failed",
                error_type=type(identity_error).__name__)
            http_status = result.pop("http_status", 503)
            result.pop("later_processing_failed", None)
            return no_store(make_response(
                render_template("result.html", **result), http_status))
        raise identity_error

    result = process_checkin(client_rec, physical, anxiety, energy, sleep, journal,
                             submission_id, "Flask Web")
    http_status = result.pop("http_status", 200)
    result.pop("later_processing_failed", None)
    return no_store(make_response(render_template("result.html", **result), http_status))


# ---------------------------------------------------------------------------
# Routes - recovery
# ---------------------------------------------------------------------------
GENERIC_RECOVERY_MESSAGE = (
    "If recovery delivery is available for that account, instructions will be sent.")


def create_recovery_request(email, client_rec=None):
    """Create the same non-enumerating recovery request used by /recover.

    Supplying a previously resolved client avoids a second Airtable lookup
    when /enroll discovers that the email is already registered. The response
    remains generic, and this helper never mutates the Client or its access
    token.
    """
    if count_recent_recovery_requests(email) >= RECOVERY_RATE_LIMIT_PER_HOUR:
        return

    now = datetime.now(timezone.utc)
    client_rec = client_rec or find_client(email=email)
    fields = {
        F_RECOVERY["request_id"]: f"{email}-{now.isoformat()}",
        F_RECOVERY["requested_at"]: now.isoformat(),
    }
    if client_rec:
        raw_recovery_token = new_random_token()
        fields[F_RECOVERY["client"]] = [client_rec["id"]]
        fields[F_RECOVERY["token_hash"]] = hash_token(raw_recovery_token)
        fields[F_RECOVERY["expires_at"]] = (
            now + timedelta(minutes=RECOVERY_TOKEN_TTL_MINUTES)).isoformat()
        # The GHL "Send Check-in Link" workflow reads this field to email the
        # link. It cannot be reconstructed from the hash, so the raw,
        # single-use, 30-minute link is stored here and cleared on redemption
        # (see recovery_redeem_submit). Airtable is already the trust boundary for
        # this app, so a short-lived recovery link is consistent with the
        # existing recovery design.
        fields[F_RECOVERY["recovery_link"]] = (
            f"{request.url_root.rstrip('/')}{recovery_fragment_path(raw_recovery_token)}")
    at_create(T_RECOVERY, fields)


@app.get("/recover")
def recover_form():
    return no_store(csrf_render("recover.html", message=None))


@app.post("/recover")
def recover_submit():
    if rate_limited("recover", client_ip()) or not valid_csrf():
        return no_store(csrf_render("recover.html", message=GENERIC_RECOVERY_MESSAGE))

    email = (request.form.get("email") or "").strip().lower()
    if email and EMAIL_RE.match(email):
        create_recovery_request(email)

    return no_store(csrf_render("recover.html", message=GENERIC_RECOVERY_MESSAGE))


@app.get("/recover-access")
def recovery_redeem_page():
    if _bearer_query_present():
        return protect_redemption_response(make_response("", 400))
    return protect_redemption_response(make_response(render_template("redeem.html")))


@app.post("/recover-access")
def recovery_redeem_submit():
    if _bearer_query_present():
        return protect_redemption_response(make_response("", 400))
    raw_token = request.form.get("token", "")
    if not valid_bearer_token(raw_token):
        return protect_redemption_response(make_response("", 400))

    token_hash = hash_token(raw_token)
    rec = find_recovery_by_token_hash(token_hash)
    if not rec:
        return protect_redemption_response(make_response("", 400))

    f = rec["fields"]
    now = datetime.now(timezone.utc)
    if f.get(F_RECOVERY["used_at"]) or _parse_iso(f[F_RECOVERY["expires_at"]]) < now:
        return protect_redemption_response(make_response("", 400))

    links = f.get(F_RECOVERY["client"]) or []
    if not links:
        return protect_redemption_response(make_response("", 400))

    at_update(T_RECOVERY, rec["id"], {F_RECOVERY["used_at"]: now.isoformat(),
                                       F_RECOVERY["recovery_link"]: ""})
    raw_access_token, expires = issue_access_token(links[0])
    resp = make_response("", 204)
    resp = set_access_cookie(resp, raw_access_token, expires)
    return protect_redemption_response(resp)


@app.get("/recover/confirm")
def legacy_recovery_confirm_rejected():
    """Bearer query parameters are intentionally no longer accepted."""
    return no_store(make_response(render_template("link_invalid.html"), 400))


# ---------------------------------------------------------------------------
# Generic error handling - participants never see raw Airtable/Gemini/GHL/
# Cloud Run error details, those go to server logs only. HTTPExceptions
# (404s, the explicit 400/401 responses above, etc.) already carry their own
# safe messages and pass through untouched.
# ---------------------------------------------------------------------------
@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    log.info(json.dumps({"event": "unhandled_error", "path": request.path,
                         "error_type": type(exc).__name__}))
    if request.path == "/assess":
        return jsonify({"error": "internal error"}), 500
    return no_store(make_response(render_template("error.html"), 500))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
