import itertools
import hashlib
import os
import sys
import types
import uuid

os.environ.setdefault("AIRTABLE_API_KEY", "test-airtable-key")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main


def submission_id_for(label):
    """Stable canonical UUIDv4 values for readable replay test fixtures."""
    raw = bytearray(hashlib.sha256(label.encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


class FakeAirtable:
    """In-memory stand-in for the CBD Core Airtable base, keyed the same
    way the real one is (table id -> {record id -> fields})."""

    def __init__(self):
        self.tables = {}
        self._counter = itertools.count(1)

    def create(self, table, fields):
        rid = f"rec{next(self._counter):014d}"
        self.tables.setdefault(table, {})[rid] = dict(fields)
        if table == main.T_ASSESS:
            log_links = fields.get(main.F_ASSESS["daily_log"])
            for log_id in log_links if isinstance(log_links, list) else []:
                log_fields = self.tables.get(main.T_LOGS, {}).get(log_id)
                if log_fields is None:
                    continue
                reverse_links = log_fields.setdefault(main.F_LOG["ai_assessments"], [])
                if isinstance(reverse_links, list) and rid not in reverse_links:
                    reverse_links.append(rid)
        return {"id": rid, "fields": dict(fields)}

    def update(self, table, record_id, fields):
        self.tables[table][record_id].update(fields)
        return {"id": record_id, "fields": dict(self.tables[table][record_id])}

    def get(self, table, record_id):
        fields = self.tables.get(table, {}).get(record_id)
        if fields is None:
            return None
        return {"id": record_id, "fields": dict(fields)}

    def find(self, table, predicate):
        for rid, fields in self.tables.get(table, {}).items():
            if predicate(fields):
                return {"id": rid, "fields": dict(fields)}
        return None

    def find_all(self, table, predicate=None):
        out = []
        for rid, fields in self.tables.get(table, {}).items():
            if predicate is None or predicate(fields):
                out.append({"id": rid, "fields": dict(fields)})
        return out


@pytest.fixture
def fake_airtable(monkeypatch):
    store = FakeAirtable()

    def at_create(table, fields):
        return store.create(table, fields)

    def at_update(table, record_id, fields):
        return store.update(table, record_id, fields)

    def at_get(table, record_id):
        return store.get(table, record_id)

    def find_client(email=None, ghl_id=None):
        if ghl_id:
            return store.find(main.T_CLIENTS, lambda f: f.get(main.F_CLIENT["ghl_id"]) == ghl_id)
        needle = (email or "").lower()
        return store.find(
            main.T_CLIENTS,
            lambda f: (f.get(main.F_CLIENT["email"]) or "").lower() == needle,
        )

    def find_client_by_access_token(raw_token):
        if not raw_token:
            return None
        token_hash = main.hash_token(raw_token)
        rec = store.find(
            main.T_CLIENTS,
            lambda f: f.get(main.F_CLIENT["access_token_hash"]) == token_hash,
        )
        if not rec:
            return None
        expires = rec["fields"].get(main.F_CLIENT["access_token_expires_at"])
        if not expires or main._parse_iso(expires) < main.datetime.now(main.timezone.utc):
            return None
        return rec

    def find_log_by_submission_id(submission_id, client_record_id):
        if not submission_id:
            return None
        records = store.find_all(
            main.T_LOGS,
            lambda f: f.get(main.F_LOG["submission_id"]) == submission_id,
        )
        return main._select_owned_log_candidate(
            records, submission_id, client_record_id,
        )

    def prior_assessments(client_record_id, n=3):
        return []

    def find_recovery_by_token_hash(token_hash):
        return store.find(
            main.T_RECOVERY, lambda f: f.get(main.F_RECOVERY["token_hash"]) == token_hash
        )

    def count_recent_recovery_requests(email, hours=1):
        needle = f"{email.lower()}-"
        return sum(
            1
            for r in store.find_all(main.T_RECOVERY)
            if r["fields"].get(main.F_RECOVERY["request_id"], "").lower().startswith(needle)
        )

    monkeypatch.setattr(main, "at_create", at_create)
    monkeypatch.setattr(main, "at_update", at_update)
    monkeypatch.setattr(main, "at_get", at_get)
    monkeypatch.setattr(main, "find_client", find_client)
    monkeypatch.setattr(main, "find_client_by_access_token", find_client_by_access_token)
    monkeypatch.setattr(main, "find_log_by_submission_id", find_log_by_submission_id)
    monkeypatch.setattr(main, "prior_assessments", prior_assessments)
    monkeypatch.setattr(main, "find_recovery_by_token_hash", find_recovery_by_token_hash)
    monkeypatch.setattr(main, "count_recent_recovery_requests", count_recent_recovery_requests)
    return store


def make_gemini_result(sentiment="neutral", progress_signal=False, distress_signal=False,
                       safety_signal="none", medical_emergency_signal=False,
                       suggested_element="earth", confidence=0.8,
                       trigger_reasons=None, summary="test summary"):
    return {
        "sentiment": sentiment,
        "progress_signal": progress_signal,
        "distress_signal": distress_signal,
        "safety_signal": safety_signal,
        "medical_emergency_signal": medical_emergency_signal,
        "trigger_reasons": trigger_reasons or ["test reason"],
        "suggested_element": suggested_element,
        "confidence": confidence,
        "summary": summary,
    }


@pytest.fixture
def mock_gemini(monkeypatch):
    """Patch main.run_assessment. Call mock_gemini.set(...) with a canned
    result dict, or mock_gemini.fail(exc) to simulate an outage/malformed
    response."""
    state = {"result": make_gemini_result(), "exc": None, "calls": 0}

    def run_assessment(journal, scores, context):
        state["calls"] += 1
        if state["exc"]:
            raise state["exc"]
        return state["result"], 120, 50, 30

    def set_result(**kwargs):
        state["result"] = make_gemini_result(**kwargs)
        state["exc"] = None

    def set_raw(raw_result):
        """Inject an arbitrary dict, bypassing make_gemini_result's
        normalization - used to simulate a malformed/incomplete response
        that fails schema validation."""
        state["result"] = raw_result
        state["exc"] = None

    def fail(exc=None):
        state["exc"] = exc or RuntimeError("Gemini unavailable")

    monkeypatch.setattr(main, "run_assessment", run_assessment)

    return types.SimpleNamespace(
        set=set_result,
        set_raw=set_raw,
        fail=fail,
        call_count=lambda: state["calls"],
    )


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """The in-process rate limiter is module-global state in main.py by
    design (single Cloud Run worker); reset it between tests so one test's
    enroll/recover calls don't 429 a later, unrelated test."""
    main._rate_buckets.clear()
    yield
    main._rate_buckets.clear()


@pytest.fixture
def client():
    main.app.config["TESTING"] = True
    # Keep our custom error handler in the response path during tests instead
    # of letting exceptions propagate out of the test client.
    main.app.config["PROPAGATE_EXCEPTIONS"] = False
    with main.app.test_client() as c:
        yield c


@pytest.fixture
def make_client_record(fake_airtable):
    def _make(email="jane@example.com", name="Jane Doe", **overrides):
        fields = {
            main.F_CLIENT["name"]: name,
            main.F_CLIENT["email"]: email,
            main.F_CLIENT["phone"]: "+15551234567",
            main.F_CLIENT["week"]: 1,
            main.F_CLIENT["state"]: "On Track",
            main.F_CLIENT["regulated_days"]: 0,
            main.F_CLIENT["sms_consent"]: True,
            main.F_CLIENT["marketing_consent"]: False,
            main.F_CLIENT["test_record"]: True,
        }
        fields.update(overrides)
        return fake_airtable.create(main.T_CLIENTS, fields)

    return _make
