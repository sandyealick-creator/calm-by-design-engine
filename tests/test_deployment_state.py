import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import validate_deployment_state as validator


DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
BASELINE = "cbd-assess-00009-mkz"
CANDIDATE = "cbd-assess-phase2d"
PROJECT = "eng-drake-502618-h6"
PROJECT_NUMBER = "185495765507"
REGION = "us-east1"
SERVICE = "cbd-assess"
SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40
BUILD_ID = "12345678-1234-1234-1234-123456789abc"
BUILD_SERVICE_ACCOUNT = "185495765507-compute@developer.gserviceaccount.com"
IMAGE_TAG = f"{REGION}-docker.pkg.dev/{PROJECT}/cbd/cbd-assess:{SOURCE_SHA}"
IMAGE_URI = IMAGE_TAG.rsplit(":", 1)[0]
REVISION_IMAGE = f"{IMAGE_URI}@{DIGEST}"
TAG_RESOURCE = (
    f"projects/{PROJECT}/locations/{REGION}/repositories/cbd/"
    f"packages/cbd-assess/tags/{SOURCE_SHA}"
)
PACKAGE_RESOURCE = (
    f"projects/{PROJECT}/locations/{REGION}/repositories/cbd/packages/cbd-assess"
)
DOCKER_IMAGE_RESOURCE = (
    f"projects/{PROJECT}/locations/{REGION}/repositories/cbd/"
    f"dockerImages/cbd-assess@{DIGEST}"
)


def explicit_build_config():
    return {
        "steps": [{
            "name": "gcr.io/cloud-builders/docker",
            "args": [
                "build", "--tag", "${_CANDIDATE_IMAGE}",
                "--label", "org.opencontainers.image.revision=${_SOURCE_SHA}",
                "--label", "com.calmbydesign.source-tree=${_SOURCE_TREE}", ".",
            ],
        }],
        "images": ["${_CANDIDATE_IMAGE}"],
        "options": {"substitutionOption": "MUST_MATCH"},
    }


def build_validation_kwargs():
    return {
        "expected_project_number": PROJECT_NUMBER,
        "expected_service_account": BUILD_SERVICE_ACCOUNT,
        "submitted_config": explicit_build_config(),
    }


def scope(project=PROJECT, region=REGION, service=SERVICE):
    return {"project": project, "region": region, "service": service}


def scoped(key, evidence, **scope_overrides):
    return {"scope": scope(**scope_overrides), key: evidence}


def revision_document(*, name=BASELINE, digest=REVISION_IMAGE, conditions=None):
    if conditions is None:
        conditions = [{"type": "Ready", "status": "True"}]
    return {
        "metadata": {"name": name},
        "status": {"conditions": conditions, "imageDigest": digest},
    }


def validate_revision(
    document,
    expected_revision=BASELINE,
    expected_digest=DIGEST,
    expected_image=IMAGE_URI,
):
    return validator.validate_revision_document(
        document,
        expected_revision,
        expected_digest,
        expected_image=expected_image,
        scope=validator.require_scope(PROJECT, REGION, SERVICE),
    )


def traffic_document(*targets, latest_ready=BASELINE, latest_created=None):
    status = {
        "latestReadyRevisionName": latest_ready,
        "traffic": list(targets),
    }
    if latest_created is not None:
        status["latestCreatedRevisionName"] = latest_created
    return {"status": status}


def fixed(revision, percent, *, tag=None):
    result = {"revisionName": revision, "percent": percent}
    if tag is not None:
        result["tag"] = tag
    return result


def latest(percent, *, tag=None):
    result = {"latestRevision": True, "percent": percent}
    if tag is not None:
        result["tag"] = tag
    return result


def session_document(*entries):
    return {"template": {"containers": [{"env": list(entries)}]}}


def session_reference(secret="session-secret", version="7"):
    return {
        "name": "SESSION_SECRET",
        "valueSource": {
            "secretKeyRef": {"secret": secret, "version": version}
        },
    }


def saved_secret_reference_result(
    *, secret="session-secret", version="7", result_scope=None
):
    return {
        "classification": "VALID_SECRET_MANAGER_REFERENCE",
        "name": "SESSION_SECRET",
        "scope": scope() if result_scope is None else result_scope,
        "secret": secret,
        "version": version,
    }


def minimal_runtime_service():
    return {
        "name": f"projects/{PROJECT}/locations/{REGION}/services/{SERVICE}",
        "template": {"containers": [{"name": "app"}]},
    }


def preserved_unnamed_runtime_service():
    return {
        "ingress": "INGRESS_TRAFFIC_ALL",
        "name": f"projects/{PROJECT}/locations/{REGION}/services/{SERVICE}",
        "scaling": {"maxInstanceCount": 20},
        "template": {
            "containers": [
                {
                    "env": [
                        {"name": "GEMINI_API_KEY"},
                        {"name": "AIRTABLE_API_KEY"},
                        {"name": "WEBHOOK_SECRET"},
                        {"name": "GEMINI_MODEL"},
                    ],
                    "ports": [{"containerPort": 8080, "name": "http1"}],
                    "resources": {
                        "cpuIdle": True,
                        "limits": {"cpu": "1000m", "memory": "512Mi"},
                        "startupCpuBoost": True,
                    },
                    "startupProbe": {
                        "failureThreshold": 1,
                        "periodSeconds": 240,
                        "tcpSocket": {"port": 8080},
                        "timeoutSeconds": 240,
                    },
                }
            ],
            "maxInstanceRequestConcurrency": 80,
            "scaling": {"maxInstanceCount": 20},
            "serviceAccount": "185495765507-compute@developer.gserviceaccount.com",
            "timeout": "300s",
        },
    }


def populated_runtime_service():
    document = minimal_runtime_service()
    document.update(
        {
            "ingress": "INGRESS_TRAFFIC_ALL",
            "invokerIamDisabled": True,
            "iapEnabled": False,
            "scaling": {
                "scalingMode": "AUTOMATIC",
                "minInstanceCount": 0,
                "maxInstanceCount": 20,
            },
        }
    )
    document["template"] = {
        "serviceAccount": "runtime@example.invalid",
        "maxInstanceRequestConcurrency": 80,
        "timeout": "300s",
        "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
        "scaling": {
            "minInstanceCount": 0,
            "maxInstanceCount": 10,
            "cpuUtilization": 0.90,
            "concurrencyUtilization": 0.95,
        },
        "vpcAccess": {
            "egress": "PRIVATE_RANGES_ONLY",
            "networkInterfaces": [
                {"network": "projects/example/global/networks/default", "tags": ["app"]}
            ],
        },
        "containers": [
            {
                "name": "app",
                "env": [
                    {"name": "PLAIN_NAME_ONLY"},
                    {
                        "name": "SESSION_SECRET",
                        "valueSource": {
                            "secretKeyRef": {"secret": "session-secret", "version": "7"}
                        },
                    },
                ],
                "resources": {
                    "limits": {"cpu": "1", "memory": "512Mi"},
                    "cpuIdle": True,
                    "startupCpuBoost": False,
                },
                "ports": [{"name": "http1", "containerPort": 8080}],
                "startupProbe": {
                    "httpGet": {
                        "path": "/health",
                        "port": 8080,
                        "httpHeaders": [{"name": "X-Probe"}],
                    },
                    "initialDelaySeconds": 0,
                    "timeoutSeconds": 1,
                    "periodSeconds": 10,
                    "failureThreshold": 3,
                },
                "livenessProbe": {"tcpSocket": {"port": 8080}},
                "readinessProbe": {
                    "httpGet": {"path": "/ready", "port": 8080},
                    "periodSeconds": 10,
                    "timeoutSeconds": 1,
                    "failureThreshold": 3,
                },
                "volumeMounts": [{"name": "cache", "mountPath": "/cache"}],
            }
        ],
        "volumes": [{"name": "cache", "emptyDir": {"medium": "MEMORY"}}],
    }
    return document


def secret_metadata(
    *,
    secret_result="FOUND",
    version_result="FOUND",
    state="ENABLED",
    project_segment=PROJECT,
):
    secret = {"result": secret_result}
    version = {"result": version_result}
    if secret_result == "FOUND":
        secret["name"] = f"projects/{project_segment}/secrets/session-secret"
    if version_result == "FOUND":
        version.update(
            {
                "name": f"projects/{project_segment}/secrets/session-secret/versions/7",
                "state": state,
            }
        )
    return {
        "requestedSecret": "session-secret",
        "requestedVersion": "7",
        "secret": secret,
        "version": version,
    }


class ValidatorTestCase(unittest.TestCase):
    def assert_validation_code(self, code, function, *args, **kwargs):
        with self.assertRaises(validator.ValidationError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class JsonSafetyTests(ValidatorTestCase):
    def test_empty_malformed_truncated_yaml_and_flattened_input(self):
        cases = [
            ("", "EMPTY_INPUT"),
            ("   ", "EMPTY_INPUT"),
            ("{broken", "MALFORMED_JSON"),
            ('{"status":', "MALFORMED_JSON"),
            ("key: value", "MALFORMED_JSON"),
            ("status.traffic[0].percent: 100", "MALFORMED_JSON"),
        ]
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.assert_validation_code(code, validator.strict_loads, raw)

    def test_duplicate_json_object_keys(self):
        self.assert_validation_code(
            "DUPLICATE_JSON_KEY",
            validator.strict_loads,
            '{"status":{},"status":{}}',
        )

    def test_scalar_and_null_are_rejected_when_an_object_is_required(self):
        for raw in ("null", "42", '"text"', "[]"):
            with self.subTest(raw=raw):
                value = validator.strict_loads(raw)
                with self.assertRaises(validator.ValidationError):
                    validate_revision(value)

    def test_structurally_unexpected_documents(self):
        for document in (None, [], "text", {"unexpected": {}}):
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validate_revision(document)


class RevisionTests(ValidatorTestCase):
    def test_exact_identity_digest_and_one_ready_true(self):
        result = validate_revision(
            revision_document(
                conditions=[
                    {"type": "ContainerHealthy", "status": "True"},
                    {"type": "Ready", "status": "True", "reason": "Ready"},
                ]
            ),
        )
        self.assertEqual(
            result,
            {
                "digest": DIGEST,
                "readiness": "READY_TRUE",
                "revision": BASELINE,
            },
        )

    def test_missing_duplicate_contradictory_and_null_readiness(self):
        cases = [
            ([], "READINESS_MISSING"),
            ([{"type": "ContainerHealthy", "status": "True"}], "READINESS_NOT_UNIQUE"),
            (
                [
                    {"type": "Ready", "status": "True"},
                    {"type": "Ready", "status": "True"},
                ],
                "CONDITION_DUPLICATE",
            ),
            (
                [
                    {"type": "Ready", "status": "True"},
                    {"type": "Ready", "status": "False"},
                ],
                "CONDITION_DUPLICATE",
            ),
            ([{"type": "Ready", "status": "False"}], "READINESS_NOT_TRUE"),
            ([{"type": "Ready", "status": None}], "CONDITION_MALFORMED"),
            ([None], "CONDITION_MALFORMED"),
        ]
        for conditions, code in cases:
            with self.subTest(code=code):
                self.assert_validation_code(
                    code,
                    validate_revision,
                    revision_document(conditions=conditions),
                )

    def test_wrong_revision_identity(self):
        self.assert_validation_code(
            "REVISION_IDENTITY_MISMATCH",
            validate_revision,
            revision_document(name=CANDIDATE),
        )

    def test_wrong_digest(self):
        self.assert_validation_code(
            "REVISION_DIGEST_MISMATCH",
            validate_revision,
            revision_document(digest=f"{IMAGE_URI}@{OTHER_DIGEST}"),
        )

    def test_null_required_revision_structure(self):
        for document in (
            {"metadata": None, "status": {}},
            {"metadata": {"name": BASELINE}, "status": None},
            {
                "metadata": {"name": BASELINE},
                "status": {"conditions": None, "imageDigest": DIGEST},
            },
        ):
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validate_revision(document)


class TrafficTests(ValidatorTestCase):
    def test_one_floating_latest_target(self):
        state = validator.parse_traffic_document(traffic_document(latest(100)))
        self.assertIn('"type":"LATEST"', state.raw_canonical())
        self.assertEqual(
            json.loads(state.effective_canonical(BASELINE)),
            [{"percent": 100, "revision": BASELINE, "tag": None}],
        )

    def test_captured_resolved_floating_latest_status_target(self):
        document = {
            "status": {
                "latestReadyRevisionName": "cbd-assess-00009-mkz",
                "traffic": [
                    {
                        "latestRevision": True,
                        "revisionName": "cbd-assess-00009-mkz",
                        "percent": 100,
                    }
                ],
            }
        }
        state = validator.parse_traffic_document(document)
        self.assertEqual(state.targets[0].target_type, "LATEST")
        self.assertIsNone(state.targets[0].revision)
        self.assertEqual(
            json.loads(state.effective_canonical("cbd-assess-00009-mkz")),
            [
                {
                    "percent": 100,
                    "revision": "cbd-assess-00009-mkz",
                    "tag": None,
                }
            ],
        )

    def test_resolved_floating_latest_must_match_latest_ready(self):
        self.assert_validation_code(
            "TRAFFIC_LATEST_RESOLUTION_MISMATCH",
            validator.parse_traffic_document,
            traffic_document(
                {
                    "latestRevision": True,
                    "revisionName": CANDIDATE,
                    "percent": 100,
                }
            ),
        )

    def test_resolved_floating_latest_requires_valid_revision_identities(self):
        resolved = {
            "latestRevision": True,
            "revisionName": BASELINE,
            "percent": 100,
        }
        self.assert_validation_code(
            "UNEXPECTED_STRUCTURE",
            validator.parse_traffic_document,
            {"status": {"traffic": [resolved]}},
        )
        for latest_ready in (None, "", "invalid_revision"):
            with self.subTest(latest_ready=latest_ready):
                self.assert_validation_code(
                    "LATEST_READY_REVISION",
                    validator.parse_traffic_document,
                    traffic_document(resolved, latest_ready=latest_ready),
                )
        for resolved_revision in (None, "", "invalid_revision"):
            with self.subTest(resolved_revision=resolved_revision):
                malformed = dict(resolved)
                malformed["revisionName"] = resolved_revision
                self.assert_validation_code(
                    "TRAFFIC_LATEST_RESOLUTION",
                    validator.parse_traffic_document,
                    traffic_document(malformed),
                )

    def test_one_fixed_revision_and_absent_tag(self):
        state = validator.parse_traffic_document(
            traffic_document(fixed(BASELINE, 100))
        )
        self.assertEqual(
            json.loads(state.raw_canonical()),
            [
                {
                    "percent": 100,
                    "revision": BASELINE,
                    "tag": None,
                    "type": "FIXED",
                }
            ],
        )

    def test_multiple_and_tagged_targets(self):
        state = validator.parse_traffic_document(
            traffic_document(
                fixed(BASELINE, 80, tag="stable"),
                fixed(CANDIDATE, 20, tag="candidate"),
            )
        )
        records = json.loads(state.raw_canonical())
        self.assertEqual({item["tag"] for item in records}, {"stable", "candidate"})

    def test_service_url_is_validated_but_not_emitted(self):
        target = fixed(BASELINE, 100)
        target["url"] = "https://service-url-must-not-appear.example"
        state = validator.parse_traffic_document(traffic_document(target))
        self.assertNotIn("service-url-must-not-appear", state.raw_canonical())
        self.assertNotIn("service-url-must-not-appear", state.effective_canonical(None))

    def test_canonical_ordering_is_deterministic(self):
        first = validator.parse_traffic_document(
            traffic_document(fixed(CANDIDATE, 30), fixed(BASELINE, 70))
        )
        second = validator.parse_traffic_document(
            traffic_document(fixed(BASELINE, 70), fixed(CANDIDATE, 30))
        )
        self.assertEqual(first.raw_canonical(), second.raw_canonical())
        self.assertEqual(first.effective_canonical(None), second.effective_canonical(None))

    def test_missing_null_duplicate_invalid_and_bad_totals(self):
        cases = [
            ([{"percent": 100}], "TRAFFIC_TARGET_TYPE"),
            ([{"revisionName": BASELINE}], "TRAFFIC_TARGET"),
            ([{"revisionName": None, "percent": 100}], "TRAFFIC_REVISION"),
            ([fixed(BASELINE, 50), fixed(BASELINE, 50)], "TRAFFIC_DUPLICATE"),
            ([fixed(BASELINE, -1), fixed(CANDIDATE, 101)], "TRAFFIC_PERCENT"),
            ([fixed(BASELINE, True)], "TRAFFIC_PERCENT"),
            ([fixed(BASELINE, 99)], "TRAFFIC_TOTAL"),
            ([fixed(BASELINE, 100), fixed(CANDIDATE, 1)], "TRAFFIC_TOTAL"),
            ([{"latestRevision": None, "percent": 100}], "TRAFFIC_TARGET_TYPE"),
            ([{"latestRevision": False, "percent": 100}], "TRAFFIC_TARGET_TYPE"),
        ]
        for targets, code in cases:
            with self.subTest(code=code):
                self.assert_validation_code(
                    code,
                    validator.parse_traffic_document,
                    traffic_document(*targets),
                )

    def test_null_scalar_and_unexpected_traffic_documents(self):
        cases = [None, [], {}, {"status": None}, {"status": {"traffic": []}}]
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.parse_traffic_document(document)

    def test_latest_resolution_requires_one_exact_revision(self):
        state = validator.parse_traffic_document(traffic_document(latest(100)))
        self.assert_validation_code(
            "LATEST_RESOLUTION", state.effective_canonical, None
        )
        self.assert_validation_code(
            "LATEST_RESOLUTION_MISMATCH", state.effective_canonical, CANDIDATE
        )

    def test_fixed_zero_traffic_transition_with_candidate_absent(self):
        result = validator.validate_zero_traffic_transition(
            traffic_document(fixed(BASELINE, 100)),
            traffic_document(
                fixed(BASELINE, 100), latest_created=CANDIDATE
            ),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )
        self.assertEqual(result["candidateTraffic"], "ABSENT")
        self.assertEqual(result["baselinePercent"], 100)
        self.assertTrue(result["effectiveAllocationPreserved"])

    def test_fixed_zero_traffic_transition_with_candidate_explicit_zero(self):
        result = validator.validate_zero_traffic_transition(
            traffic_document(fixed(BASELINE, 100)),
            traffic_document(
                fixed(BASELINE, 100), fixed(CANDIDATE, 0), latest_created=CANDIDATE
            ),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )
        self.assertEqual(result["candidateTraffic"], "EXPLICIT_ZERO")

    def test_captured_zero_traffic_service_shape_passes(self):
        candidate = "cbd-assess-17237de"
        baseline = "cbd-assess-00009-mkz"
        result = validator.validate_zero_traffic_transition(
            {
                "status": {
                    "latestReadyRevisionName": baseline,
                    "traffic": [{"revisionName": baseline, "percent": 100}],
                }
            },
            {
                "status": {
                    "latestCreatedRevisionName": candidate,
                    "latestReadyRevisionName": baseline,
                    "traffic": [{"revisionName": baseline, "percent": 100}],
                }
            },
            candidate_revision=candidate,
            baseline_revision=baseline,
            pre_latest_ready_revision=baseline,
        )
        self.assertEqual(result["latestCreatedRevision"], candidate)
        self.assertEqual(result["latestReadyRevision"], baseline)
        self.assertEqual(result["candidateTraffic"], "ABSENT")

    def test_candidate_latest_ready_with_fixed_baseline_traffic_passes(self):
        # The caller must establish this independent revision proof before
        # accepting the candidate as the allowed latest-ready identity.
        validate_revision(
            revision_document(name=CANDIDATE), expected_revision=CANDIDATE
        )
        result = validator.validate_zero_traffic_transition(
            traffic_document(fixed(BASELINE, 100)),
            traffic_document(
                fixed(BASELINE, 100),
                latest_ready=CANDIDATE,
                latest_created=CANDIDATE,
            ),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )
        self.assertEqual(result["latestCreatedRevision"], CANDIDATE)
        self.assertEqual(result["latestReadyRevision"], CANDIDATE)
        self.assertEqual(result["baselinePercent"], 100)
        self.assertEqual(result["candidateTraffic"], "ABSENT")

    def test_pre_approved_ready_candidate_with_fixed_baseline_traffic_passes(self):
        pre_ready_candidate = "cbd-assess-17237de"
        pre_ready_evidence = revision_document(name=pre_ready_candidate)
        result = validator.validate_zero_traffic_transition(
            traffic_document(
                fixed(BASELINE, 100),
                latest_ready=pre_ready_candidate,
                latest_created=pre_ready_candidate,
            ),
            traffic_document(
                fixed(BASELINE, 100),
                latest_ready=CANDIDATE,
                latest_created=CANDIDATE,
            ),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=pre_ready_candidate,
            pre_approved_revision_evidence=pre_ready_evidence,
            pre_approved_revision_digest=DIGEST,
            pre_approved_revision_image=IMAGE_URI,
            scope=validator.require_scope(PROJECT, REGION, SERVICE),
        )
        self.assertTrue(result["effectiveAllocationPreserved"])
        self.assertEqual(result["baselinePercent"], 100)

    def test_pre_approved_candidate_requires_exact_revision_evidence(self):
        pre_ready_candidate = "cbd-assess-17237de"
        pre = traffic_document(fixed(BASELINE, 100), latest_ready=pre_ready_candidate)
        post = traffic_document(fixed(BASELINE, 100), latest_created=CANDIDATE)
        common = {
            "candidate_revision": CANDIDATE,
            "baseline_revision": BASELINE,
            "pre_latest_ready_revision": pre_ready_candidate,
            "pre_approved_revision_digest": DIGEST,
            "pre_approved_revision_image": IMAGE_URI,
            "scope": validator.require_scope(PROJECT, REGION, SERVICE),
        }
        self.assert_validation_code(
            "PRE_LATEST_READY_EVIDENCE_REQUIRED",
            validator.validate_zero_traffic_transition,
            pre,
            post,
            **common,
        )
        self.assert_validation_code(
            "READINESS_NOT_TRUE",
            validator.validate_zero_traffic_transition,
            pre,
            post,
            pre_approved_revision_evidence=revision_document(
                name=pre_ready_candidate,
                conditions=[{"type": "Ready", "status": "False"}],
            ),
            **common,
        )
        self.assert_validation_code(
            "REVISION_DIGEST_MISMATCH",
            validator.validate_zero_traffic_transition,
            pre,
            post,
            pre_approved_revision_evidence=revision_document(
                name=pre_ready_candidate,
                digest=f"{IMAGE_URI}@{OTHER_DIGEST}",
            ),
            **common,
        )
        missing_digest = revision_document(name=pre_ready_candidate)
        del missing_digest["status"]["imageDigest"]
        self.assert_validation_code(
            "UNEXPECTED_STRUCTURE",
            validator.validate_zero_traffic_transition,
            pre,
            post,
            pre_approved_revision_evidence=missing_digest,
            **common,
        )

    def test_unapproved_pre_latest_ready_revision_is_rejected(self):
        self.assert_validation_code(
            "PRE_LATEST_READY_MISMATCH",
            validator.validate_zero_traffic_transition,
            traffic_document(
                fixed(BASELINE, 100),
                latest_ready="cbd-assess-unapproved",
            ),
            traffic_document(
                fixed(BASELINE, 100), latest_created=CANDIDATE
            ),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision="cbd-assess-17237de",
        )

    def test_pre_latest_ready_is_required_and_well_formed(self):
        post = traffic_document(fixed(BASELINE, 100), latest_created=CANDIDATE)
        for pre in (
            {"status": {"traffic": [fixed(BASELINE, 100)]}},
            traffic_document(fixed(BASELINE, 100), latest_ready=None),
            traffic_document(fixed(BASELINE, 100), latest_ready=123),
        ):
            with self.subTest(pre=pre):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_zero_traffic_transition(
                        pre,
                        post,
                        candidate_revision=CANDIDATE,
                        baseline_revision=BASELINE,
                        pre_latest_ready_revision=BASELINE,
                    )

    def test_pre_floating_or_tagged_traffic_is_rejected(self):
        post = traffic_document(fixed(BASELINE, 100), latest_created=CANDIDATE)
        cases = (
            ("PRE_FLOATING_LATEST", traffic_document(latest(100))),
            (
                "PRE_TAGGED_TRAFFIC",
                traffic_document(fixed(BASELINE, 100, tag="stable")),
            ),
        )
        for code, pre in cases:
            with self.subTest(code=code):
                self.assert_validation_code(
                    code,
                    validator.validate_zero_traffic_transition,
                    pre,
                    post,
                    candidate_revision=CANDIDATE,
                    baseline_revision=BASELINE,
                    pre_latest_ready_revision=BASELINE,
                )

    def test_pre_candidate_traffic_is_rejected(self):
        self.assert_validation_code(
            "EFFECTIVE_TRAFFIC_DRIFT",
            validator.validate_zero_traffic_transition,
            traffic_document(fixed(BASELINE, 99), fixed(CANDIDATE, 1)),
            traffic_document(fixed(BASELINE, 100), latest_created=CANDIDATE),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )

    def test_unexpected_effective_map_drift(self):
        self.assert_validation_code(
            "EFFECTIVE_TRAFFIC_DRIFT",
            validator.validate_zero_traffic_transition,
            traffic_document(fixed(BASELINE, 100)),
            traffic_document(
                fixed(BASELINE, 90),
                fixed("cbd-assess-unexpected", 10),
                latest_created=CANDIDATE,
            ),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )

    def test_candidate_nonzero_or_tagged_is_rejected(self):
        cases = [
            (
                "CANDIDATE_HAS_TRAFFIC",
                [fixed(BASELINE, 99), fixed(CANDIDATE, 1)],
            ),
            (
                "POST_TAGGED_TRAFFIC",
                [fixed(BASELINE, 100), fixed(CANDIDATE, 0, tag="candidate")],
            ),
        ]
        for code, targets in cases:
            with self.subTest(targets=targets):
                with self.assertRaises(validator.ValidationError) as caught:
                    validator.validate_zero_traffic_transition(
                        traffic_document(fixed(BASELINE, 100)),
                        traffic_document(*targets, latest_created=CANDIDATE),
                        candidate_revision=CANDIDATE,
                        baseline_revision=BASELINE,
                        pre_latest_ready_revision=BASELINE,
                    )
                self.assertEqual(caught.exception.code, code)

    def test_pre_tagged_traffic_is_rejected(self):
        self.assert_validation_code(
            "PRE_TAGGED_TRAFFIC",
            validator.validate_zero_traffic_transition,
            traffic_document(fixed(BASELINE, 100, tag="stable")),
            traffic_document(fixed(BASELINE, 100), latest_created=CANDIDATE),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )

    def test_raw_change_beyond_documented_transformation_is_rejected(self):
        pre = traffic_document(fixed(BASELINE, 100))
        post = traffic_document(
            fixed(BASELINE, 100),
            fixed("cbd-assess-unexpected", 0),
            latest_created=CANDIDATE,
        )
        self.assert_validation_code(
            "RAW_TRAFFIC_TRANSFORMATION",
            validator.validate_zero_traffic_transition,
            pre,
            post,
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )


class SessionSecretTests(ValidatorTestCase):
    def test_valid_reference_outputs_only_selected_metadata(self):
        document = session_document(
            {"name": "UNRELATED_PLAINTEXT_SENTINEL"},
            {
                "name": "UNRELATED_SECRET",
                "valueSource": {
                    "secretKeyRef": {
                        "secret": "unrelated-secret",
                        "version": "2",
                    }
                },
            },
            session_reference(),
        )
        result = validator.parse_session_secret_document(document)
        rendered = json.dumps(result, sort_keys=True)
        self.assertEqual(result["classification"], "VALID_SECRET_MANAGER_REFERENCE")
        self.assertEqual(result["secret"], "session-secret")
        self.assertEqual(result["version"], "7")
        self.assertNotIn("UNRELATED", rendered)

    def test_missing_duplicate_plaintext_null_and_incomplete_reference(self):
        missing_secret = session_reference()
        del missing_secret["valueSource"]["secretKeyRef"]["secret"]
        missing_version = session_reference()
        del missing_version["valueSource"]["secretKeyRef"]["version"]
        cases = [
            ([], "BLOCKER_SESSION_SECRET_MISSING"),
            (
                [session_reference(), session_reference()],
                "BLOCKER_SESSION_SECRET_DUPLICATE",
            ),
            (
                [{"name": "SESSION_SECRET"}],
                "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            ),
            (
                [{"name": "SESSION_SECRET", "valueSource": None}],
                "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            ),
            (
                [session_reference(secret=None)],
                "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            ),
            (
                [session_reference(version=None)],
                "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            ),
            (
                [missing_secret],
                "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            ),
            (
                [missing_version],
                "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            ),
        ]
        for entries, code in cases:
            with self.subTest(code=code):
                self.assert_validation_code(
                    code,
                    validator.parse_session_secret_document,
                    session_document(*entries),
                )

    def test_malformed_session_response(self):
        for document in (None, [], {}, {"template": None}):
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.parse_session_secret_document(document)

    def test_plaintext_value_rejected_without_name_or_value_disclosure(self):
        sentinel_name = "UNRELATED_NAME_MUST_NOT_APPEAR"
        sentinel_value = "PLAINTEXT_VALUE_MUST_NOT_APPEAR"
        raw = json.dumps(
            scoped(
                "serviceConfig",
                session_document(
                    {"name": sentinel_name, "value": sentinel_value},
                    session_reference(),
                ),
            )
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(validator.sys, "stdin", io.StringIO(raw)):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = validator.main(
                    [
                        "session-secret",
                        "--project",
                        PROJECT,
                        "--region",
                        REGION,
                        "--service",
                        SERVICE,
                        "--project-number",
                        PROJECT_NUMBER,
                    ]
                )
        self.assertEqual(result, 2)
        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertIn("PLAINTEXT_VALUE_REJECTED", rendered)
        self.assertNotIn(sentinel_name, rendered)
        self.assertNotIn(sentinel_value, rendered)


class SecretVersionTests(ValidatorTestCase):
    def validate(self, document):
        return validator.validate_secret_version_document(
            document,
            expected_secret="session-secret",
            expected_version="7",
            project=PROJECT,
        )

    def test_enabled_referenced_version(self):
        result = self.validate(secret_metadata())
        self.assertEqual(result["classification"], "EXISTING_ENABLED")
        self.assertEqual(result["version"], "7")

    def test_missing_disabled_and_destroyed_classifications(self):
        cases = [
            (
                secret_metadata(
                    secret_result="NOT_FOUND", version_result="NOT_FOUND"
                ),
                "MISSING_SECRET",
            ),
            (secret_metadata(version_result="NOT_FOUND"), "MISSING_VERSION"),
            (secret_metadata(state="DISABLED"), "DISABLED"),
            (secret_metadata(state="DESTROYED"), "DESTROYED"),
        ]
        for document, classification in cases:
            with self.subTest(classification=classification):
                self.assertEqual(self.validate(document)["classification"], classification)

    def test_malformed_secret_version_metadata(self):
        cases = [
            None,
            {},
            {"requestedSecret": "session-secret"},
            {
                **secret_metadata(),
                "version": {"result": "FOUND", "name": None, "state": "ENABLED"},
            },
            {
                **secret_metadata(),
                "version": {
                    "result": "FOUND",
                    "name": f"projects/{PROJECT}/secrets/session-secret/versions/7",
                    "state": "UNKNOWN",
                },
            },
        ]
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    self.validate(document)

    def test_exact_numeric_version_must_match(self):
        document = secret_metadata()
        document["requestedVersion"] = "8"
        self.assert_validation_code(
            "SECRET_VERSION_MISMATCH",
            validator.validate_secret_version_document,
            document,
            expected_secret="session-secret",
            expected_version="8",
            project=PROJECT,
        )


class Phase2FSecretSafetyTests(ValidatorTestCase):
    def test_secret_reference_grammar_accepts_only_name_or_full_resource(self):
        for secret in (
            "session-secret",
            f"projects/{PROJECT}/secrets/session-secret",
        ):
            with self.subTest(secret=secret):
                result = validator.parse_session_secret_document(
                    session_document(session_reference(secret=secret, version="7"))
                )
                self.assertEqual(result["version"], "7")

    def test_secret_reference_whitespace_control_and_value_like_text_rejected(self):
        bad_references = (
            "",
            "   ",
            " session-secret",
            "session-secret ",
            "session\nsecret",
            "session\x00secret",
            "secret=plaintext-value",
            "https://example.invalid/secret",
            f"projects/{PROJECT}/secrets/session-secret/versions/7",
        )
        for secret in bad_references:
            with self.subTest(secret=repr(secret)):
                with self.assertRaises(validator.ValidationError):
                    validator.parse_session_secret_document(
                        session_document(session_reference(secret=secret))
                    )

    def test_only_exact_numeric_version_selector_is_accepted(self):
        for version in ("", " ", "latest", "LATEST", "0", "01", "7 ", "v7"):
            with self.subTest(version=repr(version)):
                with self.assertRaises(validator.ValidationError):
                    validator.parse_session_secret_document(
                        session_document(session_reference(version=version))
                    )
                with self.assertRaises(validator.ValidationError):
                    validator.validate_secret_version_document(
                        secret_metadata(),
                        expected_secret="session-secret",
                        expected_version=version,
                        project=PROJECT,
                    )

    def test_missing_secret_requires_complete_noncontradictory_envelope(self):
        valid = secret_metadata(secret_result="NOT_FOUND", version_result="NOT_FOUND")
        self.assertEqual(
            validator.validate_secret_version_document(
                valid,
                expected_secret="session-secret",
                expected_version="7",
                project=PROJECT,
            )["classification"],
            "MISSING_SECRET",
        )
        malformed = [
            {**valid, "version": None},
            {**valid, "version": {"result": "NOT_FOUND", "state": "ENABLED"}},
            secret_metadata(secret_result="NOT_FOUND", version_result="FOUND"),
            {**valid, "secret": {"result": "NOT_FOUND", "name": "payload"}},
            {**valid, "version": {"result": "AMBIGUOUS"}},
        ]
        for document in malformed:
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_secret_version_document(
                        document,
                        expected_secret="session-secret",
                        expected_version="7",
                        project=PROJECT,
                    )


class Phase2FScopeAndTransitionTests(ValidatorTestCase):
    def test_scope_binding_rejects_each_mismatch(self):
        for override in (
            {"project": "wrong-project-123"},
            {"region": "us-west1"},
            {"service": "other-service"},
        ):
            document = scoped("revision", revision_document(), **override)
            with self.subTest(override=override):
                self.assert_validation_code(
                    "SCOPE_MISMATCH",
                    validator.scoped_payload,
                    document,
                    "revision",
                    project=PROJECT,
                    region=REGION,
                    service=SERVICE,
                )

    def test_post_service_bindings_require_created_candidate_and_allowed_ready_identity(self):
        pre = traffic_document(fixed(BASELINE, 100))
        post = traffic_document(fixed(BASELINE, 100), latest_created=CANDIDATE)
        self.assert_validation_code(
            "POST_LATEST_CREATED_MISMATCH",
            validator.validate_zero_traffic_transition,
            pre,
            traffic_document(fixed(BASELINE, 100)),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )
        self.assert_validation_code(
            "POST_LATEST_READY_MISMATCH",
            validator.validate_zero_traffic_transition,
            pre,
            traffic_document(
                fixed(BASELINE, 100),
                latest_ready="cbd-assess-unexpected",
                latest_created=CANDIDATE,
            ),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )

    def test_post_latest_ready_is_required_and_well_formed(self):
        pre = traffic_document(fixed(BASELINE, 100))
        malformed_documents = [
            {
                "status": {
                    "latestCreatedRevisionName": CANDIDATE,
                    "traffic": [fixed(BASELINE, 100)],
                }
            },
            traffic_document(
                fixed(BASELINE, 100),
                latest_ready=None,
                latest_created=CANDIDATE,
            ),
            traffic_document(
                fixed(BASELINE, 100),
                latest_ready=123,
                latest_created=CANDIDATE,
            ),
        ]
        for post in malformed_documents:
            with self.subTest(post=post):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_zero_traffic_transition(
                        pre,
                        post,
                        candidate_revision=CANDIDATE,
                        baseline_revision=BASELINE,
                        pre_latest_ready_revision=BASELINE,
                    )
        self.assert_validation_code(
            "POST_FLOATING_LATEST",
            validator.validate_zero_traffic_transition,
            pre,
            traffic_document(latest(100), latest_created=CANDIDATE),
            candidate_revision=CANDIDATE,
            baseline_revision=BASELINE,
            pre_latest_ready_revision=BASELINE,
        )


class Phase2FGateTests(ValidatorTestCase):
    def build_document(self, status="SUCCESS", **overrides):
        document = {
            "name": f"projects/{PROJECT}/locations/{REGION}/builds/{BUILD_ID}",
            "id": BUILD_ID,
            "projectId": PROJECT,
            "status": status,
            "serviceAccount": (
                f"projects/{PROJECT}/serviceAccounts/{BUILD_SERVICE_ACCOUNT}"
            ),
            "steps": [
                {
                    "name": "gcr.io/cloud-builders/docker",
                    "args": [
                        "build",
                        "--tag",
                        IMAGE_TAG,
                        "--label",
                        f"org.opencontainers.image.revision={SOURCE_SHA}",
                        "--label",
                        f"com.calmbydesign.source-tree={SOURCE_TREE}",
                        ".",
                    ],
                }
            ],
            "images": [IMAGE_TAG],
            "options": {"substitutionOption": "MUST_MATCH"},
            "createTime": "2026-08-11T12:00:00Z",
            "startTime": "2026-08-11T12:00:01Z",
            "finishTime": "2026-08-11T12:01:00Z",
            "substitutions": {
                "_SOURCE_SHA": SOURCE_SHA,
                "_SOURCE_TREE": SOURCE_TREE,
                "_CANDIDATE_IMAGE": IMAGE_TAG,
            },
            "source": {
                "storageSource": {
                    "bucket": f"{PROJECT}_cloudbuild",
                    "object": "source/authorized.tgz",
                    "generation": "123456789",
                }
            },
            "sourceProvenance": {
                "resolvedStorageSource": {
                    "bucket": f"{PROJECT}_cloudbuild",
                    "object": "source/authorized.tgz",
                    "generation": "123456789",
                }
            },
            "results": {
                "images": [
                    {
                        "name": IMAGE_TAG,
                        "digest": DIGEST,
                        "artifactRegistryPackage": f"{PACKAGE_RESOURCE}/versions/{DIGEST}",
                    }
                ]
            },
        }
        document.update(overrides)
        return document

    def test_candidate_tag_and_revision_exact_not_found_and_collision(self):
        authorized_scope = validator.require_scope(PROJECT, REGION, SERVICE)
        not_found = {
            "httpStatus": 404,
            "body": {"error": {"code": 404, "status": "NOT_FOUND"}},
        }
        for kind in ("CANDIDATE_TAG", "CANDIDATE_REVISION"):
            resource = (
                TAG_RESOURCE
                if kind == "CANDIDATE_TAG"
                else authorized_scope.service_resource + f"/revisions/{CANDIDATE}"
            )
            with self.subTest(kind=kind):
                self.assertEqual(
                    validator.validate_nonexistence_document(
                        not_found,
                        expected_kind=kind,
                        expected_resource=resource,
                        scope=authorized_scope,
                    )["classification"],
                    f"{kind}_AVAILABLE",
                )
                self.assert_validation_code(
                    f"{kind}_COLLISION",
                    validator.validate_nonexistence_document,
                    {"httpStatus": 200, "body": {"name": resource}},
                    expected_kind=kind,
                    expected_resource=resource,
                    scope=authorized_scope,
                )
        for status in (401, 403, 500, 0):
            with self.subTest(status=status):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_nonexistence_document(
                        {"httpStatus": status, "body": {}},
                        expected_kind="CANDIDATE_TAG",
                        expected_resource=TAG_RESOURCE,
                        scope=authorized_scope,
                    )

    def test_build_identifier_source_image_and_terminal_state(self):
        result = validator.validate_build_document(
            self.build_document(),
            expected_build_id=BUILD_ID,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            expected_image_tag=IMAGE_TAG,
            **build_validation_kwargs(),
            scope=validator.require_scope(PROJECT, REGION, SERVICE),
        )
        self.assertEqual(result["classification"], "BUILD_SUCCESS")
        for bad_id in ("", "not-a-uuid", "12345678-1234"):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_build_document(
                        self.build_document(),
                        expected_build_id=bad_id,
                        expected_source_sha=SOURCE_SHA,
                        expected_source_tree=SOURCE_TREE,
                        expected_image_tag=IMAGE_TAG,
                        **build_validation_kwargs(),
                        scope=validator.require_scope(PROJECT, REGION, SERVICE),
                    )
        for state in validator.NONTERMINAL_BUILD_STATES:
            with self.subTest(state=state):
                nonterminal = self.build_document(state)
                nonterminal.pop("finishTime")
                nonterminal.pop("results")
                with self.assertRaises(validator.NonterminalBuild):
                    validator.validate_build_document(
                        nonterminal,
                        expected_build_id=BUILD_ID,
                        expected_source_sha=SOURCE_SHA,
                        expected_source_tree=SOURCE_TREE,
                        expected_image_tag=IMAGE_TAG,
                        **build_validation_kwargs(),
                        scope=validator.require_scope(PROJECT, REGION, SERVICE),
                    )
        for state in validator.FAILED_BUILD_STATES | {"UNKNOWN", ""}:
            with self.subTest(state=state):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_build_document(
                        self.build_document(state),
                        expected_build_id=BUILD_ID,
                        expected_source_sha=SOURCE_SHA,
                        expected_source_tree=SOURCE_TREE,
                        expected_image_tag=IMAGE_TAG,
                        **build_validation_kwargs(),
                        scope=validator.require_scope(PROJECT, REGION, SERVICE),
                    )
        for override in (
            {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            {"images": [IMAGE_TAG + "-other"]},
            {
                "substitutions": {
                    "_SOURCE_SHA": "c" * 40,
                    "_SOURCE_TREE": SOURCE_TREE,
                    "_CANDIDATE_IMAGE": IMAGE_TAG,
                }
            },
        ):
            with self.subTest(override=override):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_build_document(
                        self.build_document(**override),
                        expected_build_id=BUILD_ID,
                        expected_source_sha=SOURCE_SHA,
                        expected_source_tree=SOURCE_TREE,
                        expected_image_tag=IMAGE_TAG,
                        **build_validation_kwargs(),
                        scope=validator.require_scope(PROJECT, REGION, SERVICE),
                    )

    def test_tag_resolution_is_unique_canonical_and_build_bound(self):
        evidence = {
            "name": DOCKER_IMAGE_RESOURCE,
            "uri": IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST,
            "tags": [SOURCE_SHA],
        }
        result = validator.validate_tag_resolution_document(
            evidence,
            expected_image_tag=IMAGE_TAG,
            expected_project_number=PROJECT_NUMBER,
            scope=validator.require_scope(PROJECT, REGION, SERVICE),
        )
        self.assertEqual(result["imageDigestRef"], IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST)
        for changed in (
            {**evidence, "name": DOCKER_IMAGE_RESOURCE + "-other"},
            {**evidence, "uri": evidence["uri"] + "," + OTHER_DIGEST},
            {**evidence, "tags": [IMAGE_TAG + "-other"]},
            [evidence, evidence],
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_tag_resolution_document(
                        changed,
                        expected_image_tag=IMAGE_TAG,
                        expected_project_number=PROJECT_NUMBER,
                        scope=validator.require_scope(PROJECT, REGION, SERVICE),
                    )

    def test_runtime_configuration_equality_and_drift(self):
        authorized_scope = validator.require_scope(PROJECT, REGION, SERVICE)
        runtime = populated_runtime_service()
        first = validator.validate_runtime_service_document(runtime, authorized_scope)
        second = validator.validate_runtime_service_document(runtime, authorized_scope)
        self.assertEqual(first, second)
        self.assertEqual(
            validator.validate_runtime_comparison(
                {"pre": runtime, "post": runtime}, authorized_scope
            )["classification"],
            "RUNTIME_UNCHANGED",
        )
        changed = json.loads(json.dumps(runtime))
        changed["template"]["maxInstanceRequestConcurrency"] = 81
        changed_hash = validator.validate_runtime_service_document(
            changed, authorized_scope
        )["sha256"]
        self.assert_validation_code(
            "RUNTIME_DRIFT",
            validator.validate_runtime_comparison,
            {"pre": runtime, "post": changed},
            authorized_scope,
        )
        for unsafe in (
            {**runtime, "name": "projects/wrong12/locations/us-east1/services/other"},
            {**runtime, "template": {"containers": [{"name": "app", "value": "ADVERSARIAL_SECRET"}]}},
            {**runtime, "template": {"containers": [{"name": "app", "unknown": "ADVERSARIAL_SECRET"}]}},
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_runtime_service_document(unsafe, authorized_scope)

    def test_exact_expected_traffic_and_rollback_maps(self):
        observed = traffic_document(fixed(BASELINE, 100))
        for purpose in ("TRAFFIC", "ROLLBACK"):
            with self.subTest(purpose=purpose):
                result = validator.validate_traffic_map_comparison(
                    {"observed": observed, "expected": observed}, purpose=purpose
                )
                self.assertEqual(result["classification"], f"{purpose}_MAP_MATCH")
                with self.assertRaises(validator.ValidationError):
                    validator.validate_traffic_map_comparison(
                        {
                            "observed": observed,
                            "expected": traffic_document(
                                fixed(CANDIDATE, 100), latest_ready=CANDIDATE
                            ),
                        },
                        purpose=purpose,
                    )
                with self.assertRaises(validator.ValidationError):
                    validator.validate_traffic_map_comparison(
                        {"observed": observed, "expected": traffic_document(latest(100))},
                        purpose=purpose,
                    )


class ExplicitBuildConfigContractTests(ValidatorTestCase):
    def setUp(self):
        self.scope = validator.require_scope(PROJECT, REGION, SERVICE)

    def config(self):
        return {
            "steps": [
                {
                    "name": "gcr.io/cloud-builders/docker",
                    "args": [
                        "build",
                        "--tag",
                        "${_CANDIDATE_IMAGE}",
                        "--label",
                        "org.opencontainers.image.revision=${_SOURCE_SHA}",
                        "--label",
                        "com.calmbydesign.source-tree=${_SOURCE_TREE}",
                        ".",
                    ],
                }
            ],
            "images": ["${_CANDIDATE_IMAGE}"],
            "options": {"substitutionOption": "MUST_MATCH"},
        }

    def validate(self, document):
        return validator.validate_build_config_document(
            document,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            expected_image_tag=IMAGE_TAG,
            scope=self.scope,
        )

    def test_exact_explicit_build_configuration_passes(self):
        self.assertEqual(
            self.validate(self.config())["classification"],
            "EXPLICIT_BUILD_CONFIG_VALID",
        )

    def test_all_three_governed_substitution_references_are_exact(self):
        config = self.config()
        rendered = json.dumps(config, sort_keys=True)
        for name in ("_SOURCE_SHA", "_SOURCE_TREE", "_CANDIDATE_IMAGE"):
            with self.subTest(name=name):
                self.assertIn("${" + name + "}", rendered)
        self.assertEqual(rendered.count("${_CANDIDATE_IMAGE}"), 2)

    def test_missing_source_sha_reference_fails(self):
        config = self.config()
        config["steps"][0]["args"].remove(
            "org.opencontainers.image.revision=${_SOURCE_SHA}"
        )
        self.assert_validation_code("BUILD_CONFIG_ARGS", self.validate, config)

    def test_unused_source_tree_substitution_mutation_fails(self):
        config = self.config()
        config["steps"][0]["args"][-2] = "com.calmbydesign.source-tree=unused"
        self.assert_validation_code("BUILD_CONFIG_ARGS", self.validate, config)

    def test_missing_or_altered_candidate_image_reference_fails(self):
        for mutate in (
            lambda item: item["steps"][0]["args"].remove("${_CANDIDATE_IMAGE}"),
            lambda item: item["steps"][0]["args"].__setitem__(
                2, "${_CANDIDATE_IMAGE}:mutable"
            ),
        ):
            with self.subTest(mutate=mutate):
                config = self.config()
                mutate(config)
                self.assert_validation_code("BUILD_CONFIG_ARGS", self.validate, config)

    def test_allow_loose_fails(self):
        config = self.config()
        config["options"]["substitutionOption"] = "ALLOW_LOOSE"
        self.assert_validation_code("BUILD_CONFIG_OPTIONS", self.validate, config)

    def test_missing_or_non_must_match_policy_fails(self):
        for options in ({}, {"substitutionOption": "SUBSTITUTION_OPTION_UNSPECIFIED"}):
            with self.subTest(options=options):
                config = self.config()
                config["options"] = options
                self.assert_validation_code(
                    "BUILD_CONFIG_OPTIONS", self.validate, config
                )

    def test_unexpected_build_step_fails(self):
        config = self.config()
        config["steps"].append(
            {"name": "gcr.io/cloud-builders/docker", "args": ["push"]}
        )
        self.assert_validation_code("BUILD_CONFIG_STEPS", self.validate, config)

    def test_unexpected_image_fails(self):
        config = self.config()
        config["images"].append("${_CANDIDATE_IMAGE}:unexpected")
        self.assert_validation_code("BUILD_CONFIG_IMAGES", self.validate, config)

    def test_docker_tag_and_top_level_image_mismatch_fails(self):
        config = self.config()
        config["images"] = [IMAGE_TAG]
        self.assert_validation_code("BUILD_CONFIG_IMAGES", self.validate, config)

    def test_altered_provenance_label_key_or_value_fails(self):
        for index, value in (
            (4, "org.opencontainers.image.ref=${_SOURCE_SHA}"),
            (6, "com.calmbydesign.source-tree=${_SOURCE_SHA}"),
        ):
            with self.subTest(index=index):
                config = self.config()
                config["steps"][0]["args"][index] = value
                self.assert_validation_code("BUILD_CONFIG_ARGS", self.validate, config)

    def test_implicit_tag_style_template_does_not_satisfy_contract(self):
        implicit = {
            "steps": [
                {
                    "name": "gcr.io/cloud-builders/docker",
                    "args": ["build", "--tag", IMAGE_TAG, "."],
                }
            ],
            "images": [IMAGE_TAG],
            "options": {"substitutionOption": "MUST_MATCH"},
        }
        self.assert_validation_code("BUILD_CONFIG_ARGS", self.validate, implicit)

    def test_unexpected_config_key_or_substitution_map_fails(self):
        for key, value in (
            ("timeout", "600s"),
            ("substitutions", {"_SOURCE_SHA": SOURCE_SHA}),
        ):
            with self.subTest(key=key):
                config = self.config()
                config[key] = value
                self.assert_validation_code(
                    "BUILD_CONFIG_STRUCTURE", self.validate, config
                )


class Phase2HRegressionTests(ValidatorTestCase):
    def setUp(self):
        self.authorized_scope = validator.require_scope(PROJECT, REGION, SERVICE)
        self.build = Phase2FGateTests().build_document()
        self.docker_image = {
            "name": DOCKER_IMAGE_RESOURCE,
            "uri": IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST,
            "tags": [SOURCE_SHA],
        }

    def validate_build(self, document):
        return validator.validate_build_document(
            document,
            expected_build_id=BUILD_ID,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            expected_image_tag=IMAGE_TAG,
            **build_validation_kwargs(),
            scope=self.authorized_scope,
        )

    def authorize(self, build=None, docker_image=None):
        return validator.validate_deployment_image_authorization(
            self.build if build is None else build,
            self.docker_image if docker_image is None else docker_image,
            expected_build_id=BUILD_ID,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            expected_image_tag=IMAGE_TAG,
            **build_validation_kwargs(),
            scope=self.authorized_scope,
        )

    def test_latest_revision_rejects_integer_zero_and_one(self):
        for value in (0, 1):
            with self.subTest(value=value):
                self.assert_validation_code(
                    "TRAFFIC_TARGET_TYPE",
                    validator.parse_traffic_document,
                    traffic_document(
                        {"revisionName": BASELINE, "latestRevision": value, "percent": 100}
                    ),
                )

    def test_latest_revision_exact_boolean_contract(self):
        self.assertEqual(
            validator.parse_traffic_document(
                traffic_document({"latestRevision": True, "percent": 100})
            ).targets[0].target_type,
            "LATEST",
        )
        self.assertEqual(
            validator.parse_traffic_document(
                traffic_document(
                    {"revisionName": BASELINE, "latestRevision": False, "percent": 100}
                )
            ).targets[0].target_type,
            "FIXED",
        )
        for value in (None, "true", "false", "0", "1"):
            with self.subTest(value=value):
                with self.assertRaises(validator.ValidationError):
                    validator.parse_traffic_document(
                        traffic_document(
                            {"revisionName": BASELINE, "latestRevision": value, "percent": 100}
                        )
                    )

    def test_impossible_build_timestamps_are_rejected(self):
        for field, value in (
            ("createTime", "2026-99-11T12:00:00Z"),
            ("startTime", "2026-02-30T12:00:00Z"),
            ("finishTime", "2026-08-11T25:00:00Z"),
        ):
            with self.subTest(field=field):
                document = json.loads(json.dumps(self.build))
                document[field] = value
                self.assert_validation_code("BUILD_TIME", self.validate_build, document)

    def test_build_timestamp_chronology_and_format(self):
        reversed_build = json.loads(json.dumps(self.build))
        reversed_build["finishTime"] = "2026-08-11T11:59:59Z"
        self.assert_validation_code("BUILD_TIME_ORDER", self.validate_build, reversed_build)
        for value in (
            " 2026-08-11T12:00:00Z",
            "2026-08-11T12:00:00.Z",
            "2026-08-11 12:00:00Z",
        ):
            document = json.loads(json.dumps(self.build))
            document["createTime"] = value
            with self.subTest(value=value):
                self.assert_validation_code("BUILD_TIME", self.validate_build, document)

    def test_successful_build_requires_one_built_image(self):
        for results in (None, {}, {"images": []}, {"images": [
            self.build["results"]["images"][0], self.build["results"]["images"][0]
        ]}):
            document = json.loads(json.dumps(self.build))
            if results is None:
                document.pop("results")
            else:
                document["results"] = results
            with self.subTest(results=results):
                with self.assertRaises(validator.ValidationError):
                    self.validate_build(document)

    def test_built_image_name_digest_and_package_are_exact(self):
        overrides = (
            ("name", IMAGE_TAG.replace("/cbd/", "/other/"), "BUILT_IMAGE_NAME"),
            ("digest", "sha256:short", "BUILT_IMAGE_DIGEST"),
            ("artifactRegistryPackage", PACKAGE_RESOURCE.replace("/cbd/", "/other/"), "BUILT_IMAGE_PACKAGE"),
        )
        for field, value, code in overrides:
            document = json.loads(json.dumps(self.build))
            document["results"]["images"][0][field] = value
            with self.subTest(field=field):
                self.assert_validation_code(code, self.validate_build, document)

    def test_manual_push_or_injected_claims_cannot_replace_build_results(self):
        document = json.loads(json.dumps(self.build))
        document.pop("results")
        document["imageDigest"] = DIGEST
        with self.assertRaises(validator.ValidationError):
            self.validate_build(document)

    def test_cross_repository_docker_image_is_rejected(self):
        changed = json.loads(json.dumps(self.docker_image))
        changed["name"] = changed["name"].replace("/repositories/cbd/", "/repositories/other/")
        with self.assertRaises(validator.ValidationError):
            validator.validate_tag_resolution_document(
                changed, expected_image_tag=IMAGE_TAG,
                expected_project_number=PROJECT_NUMBER, scope=self.authorized_scope
            )

    def test_build_and_tag_digest_mismatch_is_rejected(self):
        changed = json.loads(json.dumps(self.docker_image))
        changed["name"] = changed["name"].replace(DIGEST, OTHER_DIGEST)
        changed["uri"] = changed["uri"].replace(DIGEST, OTHER_DIGEST)
        self.assert_validation_code(
            "IMAGE_DIGEST_MISMATCH", self.authorize, docker_image=changed
        )

    def test_exact_build_and_tag_digest_agreement_is_authorized(self):
        result = self.authorize()
        self.assertEqual(result["classification"], "DEPLOYMENT_IMAGE_AUTHORIZED")
        self.assertEqual(result["imageDigestRef"], IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST)

    def test_stale_build_scope_source_or_identity_is_rejected(self):
        mutations = (
            ("id", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            ("projectId", "other-project"),
        )
        for field, value in mutations:
            document = json.loads(json.dumps(self.build))
            document[field] = value
            with self.subTest(field=field):
                with self.assertRaises(validator.ValidationError):
                    self.authorize(build=document)
        with self.assertRaises(validator.ValidationError):
            validator.validate_deployment_image_authorization(
                self.build,
                self.docker_image,
                expected_build_id=BUILD_ID,
                expected_source_sha="c" * 40,
                expected_source_tree=SOURCE_TREE,
                expected_image_tag=IMAGE_TAG,
                **build_validation_kwargs(),
                scope=self.authorized_scope,
            )

    def test_duplicate_traffic_tags_and_repeated_revisions_are_rejected(self):
        cases = (
            traffic_document(
                fixed(BASELINE, 50, tag="stable"),
                fixed(CANDIDATE, 50, tag="stable"),
            ),
            traffic_document(
                fixed(BASELINE, 50, tag="stable"),
                fixed(BASELINE, 50, tag="other"),
            ),
        )
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.parse_traffic_document(document)

    def test_map_command_is_derived_from_validated_unique_targets(self):
        expected = traffic_document(
            fixed(BASELINE, 80, tag="stable"), fixed(CANDIDATE, 20)
        )
        result = validator.validate_traffic_map_comparison(
            {"observed": expected, "expected": expected}, purpose="TRAFFIC"
        )
        self.assertEqual(
            result["commandMap"], f"{BASELINE}=80,{CANDIDATE}=20"
        )
        self.assertEqual(result["tagMap"], f"stable={BASELINE}")

    def test_duplicate_permission_and_not_found_fields_fail_before_classification(self):
        raw = (
            '{"error":{"code":403,"code":404,'
            '"status":"PERMISSION_DENIED","status":"NOT_FOUND"}}'
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = [
            "nonexistence", "--project", PROJECT, "--region", REGION,
            "--service", SERVICE, "--kind", "CANDIDATE_REVISION",
            "--expected-resource", f"projects/{PROJECT}/locations/{REGION}/services/{SERVICE}/revisions/{CANDIDATE}",
            "--http-status", "404",
        ]
        with mock.patch.object(validator.sys, "stdin", io.StringIO(raw)):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("DUPLICATE_JSON_KEY", stderr.getvalue())
        self.assertNotIn("PERMISSION_DENIED", stderr.getvalue())

    def test_evidence_root_and_file_paths_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="phase2h-path-test.") as created_root:
            root = __import__("os").path.realpath(created_root)
            safe_output = str(Path(root) / "new-output.json")
            self.assertEqual(validator.validate_evidence_root(root), root)
            self.assertEqual(
                validator.validate_evidence_file_path(root, safe_output, must_exist=False),
                safe_output,
            )
            existing = Path(root) / "existing.json"
            existing.write_text("{}", encoding="utf-8")
            self.assertEqual(
                validator.validate_evidence_file_path(root, str(existing), must_exist=True),
                str(existing),
            )
            for unsafe in (
                "relative/path",
                root + '/quote".json',
                root + "/line\nbreak.json",
                str(Path(root) / ".." / "escape.json"),
                str(Path(root) / "missing.json"),
            ):
                with self.subTest(unsafe=unsafe):
                    with self.assertRaises(validator.ValidationError):
                        validator.validate_evidence_file_path(root, unsafe, must_exist=True)
            if hasattr(os := __import__("os"), "symlink"):
                link = Path(root) / "link.json"
                os.symlink(existing, link)
                with self.assertRaises(validator.ValidationError):
                    validator.validate_evidence_file_path(root, str(link), must_exist=True)

    def test_preexisting_output_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="phase2h-output-test.") as created_root:
            root = __import__("os").path.realpath(created_root)
            existing = Path(root) / "result.json"
            existing.write_text("{}", encoding="utf-8")
            self.assert_validation_code(
                "EVIDENCE_FILE_EXISTS",
                validator.validate_evidence_file_path,
                root,
                str(existing),
                must_exist=False,
            )

    def test_identifier_and_curl_directive_injection_is_rejected(self):
        for project in ('bad"project', "bad\nproject", "bad\\project"):
            with self.subTest(project=project):
                with self.assertRaises(validator.ValidationError):
                    validator.require_scope(project, REGION, SERVICE)
        for image_tag in (
            IMAGE_TAG + '"\nurl = "https://invalid.example',
            IMAGE_TAG.replace("/cbd/", "/../"),
            " " + IMAGE_TAG,
        ):
            with self.subTest(image_tag=image_tag):
                with self.assertRaises(validator.ValidationError):
                    validator._image_identity(image_tag, self.authorized_scope)

    def test_authorize_image_cli_revalidates_strict_raw_files(self):
        with tempfile.TemporaryDirectory(prefix="phase2h-authorize-test.") as created_root:
            root = __import__("os").path.realpath(created_root)
            build_file = Path(root) / "build.json"
            tag_file = Path(root) / "tag.json"
            config_file = Path(root) / "cloudbuild.json"
            build_file.write_text(json.dumps(self.build), encoding="utf-8")
            tag_file.write_text(json.dumps(self.docker_image), encoding="utf-8")
            config_bytes = json.dumps(explicit_build_config()).encode("utf-8")
            config_file.write_bytes(config_bytes)
            args = [
                "authorize-image", "--project", PROJECT, "--region", REGION,
                "--service", SERVICE, "--evidence-root", root,
                "--build-evidence-file", str(build_file),
                "--tag-evidence-file", str(tag_file),
                "--expected-build-id", BUILD_ID,
                "--expected-source-sha", SOURCE_SHA,
                "--expected-source-tree", SOURCE_TREE,
                "--expected-image-tag", IMAGE_TAG,
                "--project-number", PROJECT_NUMBER,
                "--expected-service-account", BUILD_SERVICE_ACCOUNT,
                "--build-config-file", str(config_file),
                "--expected-build-config-sha256", hashlib.sha256(config_bytes).hexdigest(),
                "--output", "image-ref",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
            self.assertEqual(status, 0)
            self.assertEqual(
                stdout.getvalue().strip(), IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST
            )
            self.assertEqual(stderr.getvalue(), "")
            build_file.write_text(
                '{"name":"safe","status":"SUCCESS","status":"FAILURE"}',
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
            self.assertEqual(status, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("DUPLICATE_JSON_KEY", stderr.getvalue())
            self.assertNotIn("FAILURE", stderr.getvalue())

    def test_strict_file_parser_protects_map_runtime_and_transition_inputs(self):
        with tempfile.TemporaryDirectory(prefix="phase2h-strict-file-test.") as created_root:
            root = __import__("os").path.realpath(created_root)
            ambiguous = Path(root) / "ambiguous.json"
            ambiguous.write_text('{"status":{},"status":{}}', encoding="utf-8")
            self.assert_validation_code(
                "DUPLICATE_JSON_KEY", validator._strict_load_path, str(ambiguous)
            )

    def test_secret_name_255_and_256_character_boundaries(self):
        valid = "s" * 255
        invalid = "s" * 256
        for value in (valid, f"projects/{PROJECT}/secrets/{valid}"):
            self.assertEqual(validator._validate_secret_reference(value), value)
        for value in (invalid, f"projects/{PROJECT}/secrets/{invalid}"):
            with self.assertRaises(validator.ValidationError):
                validator._validate_secret_reference(value)

    def test_documentation_uses_current_provenance_contract(self):
        runbook = Path("DEPLOYMENT_RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("results.images", runbook)
        self.assertIn("authorize-image", runbook)
        self.assertIn("build-config", runbook)
        self.assertIn('"substitutionOption": "MUST_MATCH"', runbook)
        self.assertNotIn("--tag=\"$CANDIDATE_IMAGE_TAG\"", runbook)
        self.assertIn("`ALLOW_LOOSE` is prohibited", runbook)
        self.assertNotIn('tag.update({"buildId"', runbook)

    def test_documented_curl_authentication_fails_before_request(self):
        runbook = Path("DEPLOYMENT_RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("authorized_curl()", runbook)
        self.assertIn('curl_config="$(emit_bearer_config)" || return 1', runbook)
        self.assertIn("curl --config <(printf '%s\\n' \"$curl_config\") \"$@\"", runbook)
        self.assertNotIn("emit_bearer_config | curl", runbook)
        self.assertEqual(runbook.count("authorized_curl --"), 8)


class Phase2JRegressionTests(ValidatorTestCase):
    def setUp(self):
        self.authorized_scope = validator.require_scope(PROJECT, REGION, SERVICE)
        self.build = Phase2FGateTests().build_document()

    def validate_build(self, document):
        return validator.validate_build_document(
            document,
            expected_build_id=BUILD_ID,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            expected_image_tag=IMAGE_TAG,
            **build_validation_kwargs(),
            scope=self.authorized_scope,
        )

    def invoke_build_raw(self, raw):
        with tempfile.TemporaryDirectory(prefix="phase2j-config-test.") as created_root:
            root = __import__("os").path.realpath(created_root)
            config_file = Path(root) / "cloudbuild.json"
            config_bytes = json.dumps(explicit_build_config()).encode("utf-8")
            config_file.write_bytes(config_bytes)
            args = [
                "build", "--project", PROJECT, "--region", REGION,
                "--service", SERVICE, "--expected-build-id", BUILD_ID,
                "--expected-source-sha", SOURCE_SHA,
                "--expected-source-tree", SOURCE_TREE,
                "--expected-image-tag", IMAGE_TAG, "--project-number", PROJECT_NUMBER,
                "--expected-service-account", BUILD_SERVICE_ACCOUNT,
                "--evidence-root", root, "--build-config-file", str(config_file),
                "--expected-build-config-sha256", hashlib.sha256(config_bytes).hexdigest(),
                "--raw",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(validator.sys, "stdin", io.StringIO(raw)):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = validator.main(args)
            return status, stdout.getvalue(), stderr.getvalue()

    def test_nanosecond_reversals_at_seventh_eighth_and_ninth_digits_fail(self):
        for create_time, start_time in (
            ("2026-08-11T12:00:00.000000900Z", "2026-08-11T12:00:00.000000100Z"),
            ("2026-08-11T12:00:00.000000090Z", "2026-08-11T12:00:00.000000010Z"),
            ("2026-08-11T12:00:00.000000009Z", "2026-08-11T12:00:00.000000001Z"),
        ):
            document = json.loads(json.dumps(self.build))
            document["createTime"] = create_time
            document["startTime"] = start_time
            with self.subTest(create_time=create_time):
                self.assert_validation_code(
                    "BUILD_TIME_ORDER", self.validate_build, document
                )

    def test_timestamp_precision_generated_forms_equality_and_offsets(self):
        values = (
            "2026-08-11T12:00:00Z",
            "2026-08-11T12:00:00.123Z",
            "2026-08-11T12:00:00.123456Z",
            "2026-08-11T12:00:00.123456789Z",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsInstance(
                    validator._parse_build_timestamp(value, "test"),
                    validator.ExactTimestamp,
                )
        instant = validator._parse_build_timestamp(
            "2026-08-11T12:00:00.123456789Z", "test"
        )
        self.assertEqual(
            instant,
            validator._parse_build_timestamp(
                "2026-08-11T07:00:00.123456789-05:00", "test"
            ),
        )
        equal = json.loads(json.dumps(self.build))
        equal["createTime"] = "2026-08-11T12:00:00.000000009Z"
        equal["startTime"] = "2026-08-11T07:00:00.000000009-05:00"
        self.assertEqual(self.validate_build(equal)["classification"], "BUILD_SUCCESS")

    def test_timestamp_invalid_precision_calendar_and_offsets_fail(self):
        invalid = (
            "2026-08-11T12:00:00.1234567890Z",
            "2026-08-11T12:00:00.Z",
            "2026-08-11T12:00:00..1Z",
            "2025-02-29T12:00:00Z",
            "2026-08-11T12:00:00+24:00",
            "2026-08-11T12:00:00-05:60",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assert_validation_code(
                    "BUILD_TIME", validator._parse_build_timestamp, value, "test"
                )
        self.assertIsInstance(
            validator._parse_build_timestamp("2024-02-29T12:00:00Z", "test"),
            validator.ExactTimestamp,
        )

    def test_built_image_digest_is_field_specific_and_canonical(self):
        self.assertEqual(validator._canonical_digest(DIGEST, "TEST_DIGEST"), DIGEST)
        invalid = (
            "prefix@" + DIGEST,
            "@" + DIGEST,
            DIGEST.upper(),
            " " + DIGEST,
            DIGEST + " ",
            DIGEST + "@" + OTHER_DIGEST,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha512:" + "a" * 64,
            DIGEST + "\ntrailing",
        )
        for value in invalid:
            with self.subTest(value=repr(value)):
                self.assert_validation_code(
                    "TEST_DIGEST", validator._canonical_digest, value, "TEST_DIGEST"
                )
                document = json.loads(json.dumps(self.build))
                document["results"]["images"][0]["digest"] = value
                status, stdout, stderr = self.invoke_build_raw(json.dumps(document))
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("BUILT_IMAGE_DIGEST", stderr)

    def test_push_timing_exact_structure_chronology_and_duplicate_keys(self):
        valid = json.loads(json.dumps(self.build))
        valid["results"]["images"][0]["pushTiming"] = {
            "startTime": "2026-08-11T12:00:01.000000001Z",
            "endTime": "2026-08-11T07:00:01.000000009-05:00",
        }
        self.assertEqual(self.validate_build(valid)["classification"], "BUILD_SUCCESS")
        reversed_timing = json.loads(json.dumps(valid))
        reversed_timing["results"]["images"][0]["pushTiming"] = {
            "startTime": "2026-08-11T12:00:01.000000009Z",
            "endTime": "2026-08-11T12:00:01.000000001Z",
        }
        self.assert_validation_code(
            "BUILT_IMAGE_PUSH_TIMING_ORDER", self.validate_build, reversed_timing
        )
        malformed = (
            "timing", [], None, True, 1,
            {}, {"startTime": "2026-08-11T12:00:01Z"},
            {"endTime": "2026-08-11T12:00:01Z"},
            {"startTime": "bad", "endTime": "2026-08-11T12:00:01Z"},
            {
                "startTime": "2026-08-11T12:00:01Z",
                "endTime": "2026-08-11T12:00:02Z",
                "extra": "field",
            },
        )
        for timing in malformed:
            document = json.loads(json.dumps(self.build))
            document["results"]["images"][0]["pushTiming"] = timing
            with self.subTest(timing=timing):
                with self.assertRaises(validator.ValidationError):
                    self.validate_build(document)
        raw = json.dumps(valid, separators=(",", ":"))
        raw = raw.replace(
            '"pushTiming":{"startTime":"2026-08-11T12:00:01.000000001Z",',
            '"pushTiming":{"startTime":"2026-08-11T12:00:01.000000001Z",'
            '"startTime":"2026-08-11T12:00:01.000000001Z",',
        )
        status, stdout, stderr = self.invoke_build_raw(raw)
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("DUPLICATE_JSON_KEY", stderr)

    def test_built_image_package_and_oci_media_type_are_exact(self):
        expected_version = PACKAGE_RESOURCE + "/versions/" + DIGEST
        self.assertEqual(self.validate_build(self.build)["packageVersionResource"], expected_version)
        for package in (
            PACKAGE_RESOURCE,
            PACKAGE_RESOURCE + "/versions/" + OTHER_DIGEST,
            PACKAGE_RESOURCE.replace(f"projects/{PROJECT}/", "projects/other-project/") ,
        ):
            document = json.loads(json.dumps(self.build))
            document["results"]["images"][0]["artifactRegistryPackage"] = package
            with self.subTest(package=package):
                self.assert_validation_code(
                    "BUILT_IMAGE_PACKAGE", self.validate_build, document
                )
        for media_type in (None, "", "image/manifest", 1, True, []):
            document = json.loads(json.dumps(self.build))
            document["results"]["images"][0]["ociMediaType"] = media_type
            with self.subTest(media_type=media_type):
                self.assert_validation_code(
                    "BUILT_IMAGE_OCI_MEDIA_TYPE", self.validate_build, document
                )

    def test_invalid_secret_version_precedes_every_http_classification(self):
        not_found = {"error": {"code": 404, "status": "NOT_FOUND"}}
        invalid_versions = (
            "", "latest", "0", "01", "+1", "-1", "1.0", "1e1",
            " 1", "1 ", "1\n", "version=1", "secret-value-like-text",
        )
        paths = (
            (404, None, not_found, None),
            (200, 200, {"name": "malformed"}, {"name": "malformed"}),
            (403, None, {}, None),
            (200, 200, "malformed", "malformed"),
            (500, None, {}, None),
        )
        for version in invalid_versions:
            for secret_status, version_status, secret_body, version_body in paths:
                with self.subTest(version=repr(version), status=secret_status):
                    self.assert_validation_code(
                        "VERSION_SELECTOR",
                        validator.validate_secret_http_evidence,
                        secret_body,
                        version_body,
                        secret_status=secret_status,
                        version_status=version_status,
                        expected_secret="session-secret",
                        expected_version=version,
                        project=PROJECT,
                    )

    def test_invalid_secret_version_exact_404_cli_never_classifies_missing(self):
        with tempfile.TemporaryDirectory(prefix="phase2j-secret-test.") as created_root:
            root = __import__("os").path.realpath(created_root)
            secret_file = Path(root) / "secret.json"
            secret_file.write_text(
                json.dumps({"error": {"code": 404, "status": "NOT_FOUND"}}),
                encoding="utf-8",
            )
            args = [
                "secret-version", "--project", PROJECT, "--region", REGION,
                "--service", SERVICE, "--project-number", PROJECT_NUMBER,
                "--expected-secret", "session-secret",
                "--expected-version", "latest", "--evidence-root", root,
                "--secret-status", "404", "--version-status", "SKIPPED",
                "--secret-evidence-file", str(secret_file),
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
            self.assertEqual(status, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("VERSION_SELECTOR", stderr.getvalue())
            self.assertNotIn("MISSING_SECRET", stderr.getvalue())


class Phase2QRegressionTests(ValidatorTestCase):
    def setUp(self):
        self.authorized_scope = validator.require_scope(PROJECT, REGION, SERVICE)
        self.build = Phase2FGateTests().build_document()

    def validate_build(self, document):
        return validator.validate_build_document(
            document,
            expected_build_id=BUILD_ID,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            expected_image_tag=IMAGE_TAG,
            **build_validation_kwargs(),
            scope=self.authorized_scope,
        )

    def test_authoritative_project_identity_pair_is_exact_and_active(self):
        metadata = {
            "projectId": PROJECT,
            "projectNumber": PROJECT_NUMBER,
            "lifecycleState": "ACTIVE",
        }
        result = validator.validate_project_identity_document(
            metadata,
            expected_project_id=PROJECT,
            expected_project_number=PROJECT_NUMBER,
        )
        self.assertEqual(result["classification"], "VERIFIED_ACTIVE_PROJECT_IDENTITY")
        self.assertEqual(result["projectId"], PROJECT)
        self.assertEqual(result["projectNumber"], PROJECT_NUMBER)
        for changed in (
            {**metadata, "projectId": "other-project-123"},
            {**metadata, "projectNumber": "999999999999"},
            {**metadata, "lifecycleState": "DELETE_REQUESTED"},
            {**metadata, "unexpected": True},
            {key: value for key, value in metadata.items() if key != "projectNumber"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_project_identity_document(
                        changed,
                        expected_project_id=PROJECT,
                        expected_project_number=PROJECT_NUMBER,
                    )
        for malformed_number in (None, 185495765507, "", "0", "01", "+1", "12345", "1" * 31):
            with self.subTest(malformed_number=malformed_number):
                self.assert_validation_code(
                    "PROJECT_NUMBER",
                    validator.validate_project_identity_document,
                    metadata,
                    expected_project_id=PROJECT,
                    expected_project_number=malformed_number,
                )

    def test_secret_resources_accept_only_the_verified_project_alias_pair(self):
        for project_segment in (PROJECT, PROJECT_NUMBER):
            document = secret_metadata(project_segment=project_segment)
            with self.subTest(project_segment=project_segment):
                result = validator.validate_secret_version_document(
                    document,
                    expected_secret="session-secret",
                    expected_version="7",
                    project=PROJECT,
                    project_number=PROJECT_NUMBER,
                )
                self.assertEqual(result["classification"], "EXISTING_ENABLED")
                self.assertEqual(
                    result["secret"],
                    f"projects/{PROJECT}/secrets/session-secret",
                )
                self.assertEqual(
                    result["observedSecretResource"],
                    f"projects/{project_segment}/secrets/session-secret",
                )
                self.assertEqual(
                    result["observedVersionResource"],
                    f"projects/{project_segment}/secrets/session-secret/versions/7",
                )
                self.assertEqual(
                    result["matchedSecretProjectSegment"], project_segment
                )
                self.assertEqual(
                    result["matchedVersionProjectSegment"], project_segment
                )

    def test_numeric_secret_resource_requires_the_verified_project_number(self):
        numeric = secret_metadata(project_segment=PROJECT_NUMBER)
        self.assert_validation_code(
            "PROJECT_NUMBER_REQUIRED",
            validator.validate_secret_version_document,
            numeric,
            expected_secret="session-secret",
            expected_version="7",
            project=PROJECT,
        )
        for project_segment in ("other-project-123", "999999999999"):
            with self.subTest(project_segment=project_segment):
                self.assert_validation_code(
                    "SECRET_SCOPE_MISMATCH",
                    validator.validate_secret_version_document,
                    secret_metadata(project_segment=project_segment),
                    expected_secret="session-secret",
                    expected_version="7",
                    project=PROJECT,
                    project_number=PROJECT_NUMBER,
                )

    def test_matching_secret_name_under_another_project_is_rejected(self):
        for project_segment in ("unrelated-project-1", "999999999999"):
            document = secret_metadata(project_segment=project_segment)
            with self.subTest(project_segment=project_segment):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_secret_http_evidence(
                        {"name": document["secret"]["name"]},
                        {
                            "name": document["version"]["name"],
                            "state": "ENABLED",
                        },
                        secret_status=200,
                        version_status=200,
                        expected_secret="session-secret",
                        expected_version="7",
                        project=PROJECT,
                        project_number=PROJECT_NUMBER,
                    )

    def test_numeric_saved_reference_preserves_observed_identity(self):
        numeric_reference = saved_secret_reference_result(
            secret=f"projects/{PROJECT_NUMBER}/secrets/session-secret"
        )
        result = validator.validate_secret_reference_result(
            numeric_reference, self.authorized_scope, PROJECT_NUMBER
        )
        self.assertEqual(
            result["secretResource"], f"projects/{PROJECT}/secrets/session-secret"
        )
        self.assertEqual(
            result["observedSecretReference"],
            f"projects/{PROJECT_NUMBER}/secrets/session-secret",
        )
        self.assertEqual(result["matchedProjectSegment"], PROJECT_NUMBER)

    def test_project_alias_evidence_remains_strict(self):
        with self.assertRaises(validator.ValidationError):
            validator.strict_loads(
                '{"name":"projects/%s/secrets/session-secret",'
                '"name":"projects/%s/secrets/session-secret"}'
                % (PROJECT, PROJECT_NUMBER)
            )
        numeric = secret_metadata(project_segment=PROJECT_NUMBER)
        numeric["version"]["unexpected"] = True
        with self.assertRaises(validator.ValidationError):
            validator.validate_secret_version_document(
                numeric,
                expected_secret="session-secret",
                expected_version="7",
                project=PROJECT,
                project_number=PROJECT_NUMBER,
            )

    def test_saved_secret_reference_result_is_scope_bound_and_canonical(self):
        valid = validator.validate_secret_reference_result(
            saved_secret_reference_result(), self.authorized_scope
        )
        self.assertEqual(
            valid["secretResource"], f"projects/{PROJECT}/secrets/session-secret"
        )
        self.assertEqual(valid["version"], "7")
        full = validator.validate_secret_reference_result(
            saved_secret_reference_result(
                secret=f"projects/{PROJECT}/secrets/session-secret"
            ),
            self.authorized_scope,
        )
        self.assertEqual(full["secretResource"], valid["secretResource"])

    def test_saved_secret_reference_result_rejects_stale_or_cross_scope(self):
        stale_scopes = (
            scope(project="other-project"),
            scope(region="us-west1"),
            scope(service="other-service"),
        )
        for stale_scope in stale_scopes:
            with self.subTest(stale_scope=stale_scope):
                self.assert_validation_code(
                    "SECRET_REFERENCE_SCOPE_MISMATCH",
                    validator.validate_secret_reference_result,
                    saved_secret_reference_result(result_scope=stale_scope),
                    self.authorized_scope,
                )
        self.assert_validation_code(
            "SECRET_SCOPE_MISMATCH",
            validator.validate_secret_reference_result,
            saved_secret_reference_result(
                secret="projects/other-project/secrets/session-secret"
            ),
            self.authorized_scope,
        )

    def test_saved_secret_reference_result_rejects_versions_and_bad_envelopes(self):
        for version in ("latest", "0", "-1", "+1", "01", "1.0", 7, None, []):
            with self.subTest(version=version):
                self.assert_validation_code(
                    "SECRET_REFERENCE_VERSION",
                    validator.validate_secret_reference_result,
                    saved_secret_reference_result(version=version),
                    self.authorized_scope,
                )
        for secret in ("bad/secret", " secret", "secret\nname", None, [], {}):
            with self.subTest(secret=secret):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_secret_reference_result(
                        saved_secret_reference_result(secret=secret),
                        self.authorized_scope,
                    )
        valid = saved_secret_reference_result()
        malformed = [
            None,
            [],
            "text",
            {key: value for key, value in valid.items() if key != "version"},
            {**valid, "unexpected": True},
            {**valid, "classification": "TAMPERED"},
            {**valid, "name": "OTHER_SECRET"},
            {**valid, "scope": []},
            {**valid, "scope": {**scope(), "unexpected": True}},
        ]
        for document in malformed:
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_secret_reference_result(
                        document, self.authorized_scope
                    )

    def test_saved_secret_reference_cli_revalidates_before_output(self):
        with tempfile.TemporaryDirectory(prefix="phase2q-secret-result.") as created_root:
            root = __import__("os").path.realpath(created_root)
            result_file = Path(root) / "reference.json"
            result_file.write_text(
                json.dumps(saved_secret_reference_result()), encoding="utf-8"
            )
            args = [
                "secret-reference-result",
                "--project", PROJECT,
                "--region", REGION,
                "--service", SERVICE,
                "--project-number", PROJECT_NUMBER,
                "--evidence-root", root,
                "--input-file", str(result_file),
                "--output", "resource-version",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
            self.assertEqual(status, 0)
            self.assertEqual(
                stdout.getvalue(), f"projects/{PROJECT}/secrets/session-secret 7\n"
            )
            self.assertEqual(stderr.getvalue(), "")
            result_file.write_text(
                json.dumps(saved_secret_reference_result(result_scope=scope(service="stale"))),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
            self.assertEqual(status, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn("session-secret", stderr.getvalue())

    def test_runtime_minimal_populated_equality_and_difference(self):
        minimal = validator.validate_runtime_service_document(
            minimal_runtime_service(), self.authorized_scope
        )
        populated = validator.validate_runtime_service_document(
            populated_runtime_service(), self.authorized_scope
        )
        self.assertEqual(minimal["classification"], "RUNTIME_CANONICAL")
        self.assertEqual(populated["classification"], "RUNTIME_CANONICAL")
        self.assertEqual(
            validator.validate_runtime_comparison(
                {"pre": populated_runtime_service(), "post": populated_runtime_service()},
                self.authorized_scope,
            )["classification"],
            "RUNTIME_UNCHANGED",
        )
        self.assert_validation_code(
            "RUNTIME_DRIFT",
            validator.validate_runtime_comparison,
            {"pre": minimal_runtime_service(), "post": populated_runtime_service()},
            self.authorized_scope,
        )

    def test_runtime_rejects_empty_missing_and_type_confused_containers(self):
        service_name = self.authorized_scope.service_resource
        invalid = (
            {"name": service_name, "template": {}},
            {"name": service_name, "template": {"containers": []}},
            {"name": service_name, "template": {"containers": "not-a-list"}},
            {"name": service_name, "template": {"containers": {}}},
            {"name": service_name, "template": {"containers": None}},
            {"name": service_name, "template": {"containers": 1}},
            {"name": service_name, "template": {"containers": [{"name": None}]}},
        )
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )

    def test_runtime_accepts_exact_preserved_unnamed_singleton_projection(self):
        preserved = preserved_unnamed_runtime_service()
        result = validator.validate_runtime_service_document(
            preserved, self.authorized_scope
        )
        self.assertEqual(result["classification"], "RUNTIME_CANONICAL")

    def test_runtime_singleton_name_is_optional_and_not_drift(self):
        unnamed = minimal_runtime_service()
        unnamed["template"]["containers"][0].pop("name")
        named = minimal_runtime_service()
        for pre, post in ((unnamed, named), (named, unnamed), (unnamed, unnamed)):
            with self.subTest(pre_named="name" in pre["template"]["containers"][0]):
                self.assertEqual(
                    validator.validate_runtime_comparison(
                        {"pre": pre, "post": post}, self.authorized_scope
                    )["classification"],
                    "RUNTIME_UNCHANGED",
                )

    def test_runtime_singleton_rejects_invalid_present_names(self):
        for invalid_name in ("", "bad/name", " leading", 7, None, [], {}):
            document = minimal_runtime_service()
            document["template"]["containers"][0]["name"] = invalid_name
            with self.subTest(invalid_name=invalid_name):
                self.assert_validation_code(
                    "RUNTIME_CONTAINER",
                    validator.validate_runtime_service_document,
                    document,
                    self.authorized_scope,
                )

    def test_runtime_multiple_containers_require_unique_valid_names(self):
        valid = minimal_runtime_service()
        valid["template"]["containers"].append({"name": "sidecar"})
        self.assertEqual(
            validator.validate_runtime_service_document(
                valid, self.authorized_scope
            )["classification"],
            "RUNTIME_CANONICAL",
        )
        for containers in (
            [{"name": "app"}, {}],
            [{"name": "app"}, {"name": "app"}],
            [{"name": "app"}, {"name": ""}],
            [{"name": "app"}, {"name": None}],
        ):
            document = minimal_runtime_service()
            document["template"]["containers"] = containers
            with self.subTest(containers=containers):
                self.assert_validation_code(
                    "RUNTIME_CONTAINER",
                    validator.validate_runtime_service_document,
                    document,
                    self.authorized_scope,
                )

    def test_runtime_multiple_containers_compare_by_name_not_order(self):
        first = minimal_runtime_service()
        first["template"]["containers"] = [
            {"name": "app", "env": [{"name": "APP_ENV"}]},
            {"name": "sidecar", "env": [{"name": "SIDECAR_ENV"}]},
        ]
        reordered = json.loads(json.dumps(first))
        reordered["template"]["containers"].reverse()
        self.assertEqual(
            validator.validate_runtime_comparison(
                {"pre": first, "post": reordered}, self.authorized_scope
            )["classification"],
            "RUNTIME_UNCHANGED",
        )
        changed = json.loads(json.dumps(reordered))
        changed["template"]["containers"][0]["env"][0]["name"] = "DRIFTED"
        self.assert_validation_code(
            "RUNTIME_DRIFT",
            validator.validate_runtime_comparison,
            {"pre": first, "post": changed},
            self.authorized_scope,
        )

    def test_runtime_unnamed_singleton_preserves_unrelated_fail_closed_checks(self):
        for mutation in (
            lambda container: container.update({"unexpected": True}),
            lambda container: container.update({"ports": []}),
            lambda container: container.update({"env": [{"name": ""}]}),
        ):
            document = preserved_unnamed_runtime_service()
            mutation(document["template"]["containers"][0])
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )

    def test_runtime_rejects_malformed_nested_projected_fields(self):
        cases = []
        for mutator in (
            lambda d: d["template"]["containers"][0].update({"env": "bad"}),
            lambda d: d["template"]["containers"][0].update(
                {"env": [{"name": "SESSION_SECRET", "valueSource": {"secretKeyRef": {"secret": "bad/path", "version": "7"}}}]}
            ),
            lambda d: d["template"]["containers"][0].update({"resources": {"limits": []}}),
            lambda d: d["template"]["containers"][0].update({"resources": {"cpuIdle": "true"}}),
            lambda d: d["template"]["containers"][0].update({"startupProbe": {"httpGet": {}, "tcpSocket": {}}}),
            lambda d: d["template"]["containers"][0].update({"livenessProbe": {"tcpSocket": {"port": 0}}}),
            lambda d: d["template"]["containers"][0].update({"volumeMounts": [{"name": "cache"}]}),
            lambda d: d["template"].update({"volumes": [{"name": "cache"}]}),
            lambda d: d["template"].update({"volumes": [{"name": "cache", "emptyDir": {}, "gcs": {"bucket": "b"}}]}),
            lambda d: d["template"].update({"scaling": {"minInstanceCount": 2, "maxInstanceCount": 1}}),
            lambda d: d["template"].update({"vpcAccess": {"networkInterfaces": [{}]}}),
            lambda d: d["template"].update({"timeout": "five minutes"}),
            lambda d: d.update({"invokerIamDisabled": 1}),
        ):
            document = minimal_runtime_service()
            mutator(document)
            cases.append(document)
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )

    def test_identical_malformed_runtime_files_cannot_compare_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="phase2q-runtime.") as created_root:
            root = __import__("os").path.realpath(created_root)
            malformed = {"name": self.authorized_scope.service_resource, "template": {}}
            pre = Path(root) / "pre.json"
            post = Path(root) / "post.json"
            pre.write_text(json.dumps(malformed), encoding="utf-8")
            post.write_text(json.dumps(malformed), encoding="utf-8")
            args = [
                "runtime-equal", "--project", PROJECT, "--region", REGION,
                "--service", SERVICE, "--evidence-root", root,
                "--pre-evidence-file", str(pre), "--post-evidence-file", str(post),
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
            self.assertEqual(status, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("RUNTIME_TEMPLATE", stderr.getvalue())

    def test_service_scaling_is_typed_optional_and_drift_sensitive(self):
        absent = minimal_runtime_service()
        automatic = minimal_runtime_service()
        automatic["scaling"] = {
            "scalingMode": "AUTOMATIC",
            "minInstanceCount": 0,
            "maxInstanceCount": 20,
        }
        manual = minimal_runtime_service()
        manual["scaling"] = {
            "scalingMode": "MANUAL",
            "manualInstanceCount": 2,
        }
        self.assertEqual(
            validator.validate_runtime_service_document(
                absent, self.authorized_scope
            )["classification"],
            "RUNTIME_CANONICAL",
        )
        for label, document in (("automatic", automatic), ("manual", manual)):
            with self.subTest(valid_scaling=label):
                self.assertEqual(
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )["classification"],
                    "RUNTIME_CANONICAL",
                )

        # GoogleCloudRunV2ServiceScaling.ScalingModeValueValuesEnum in the
        # locally generated Run v2 messages, enumerated independently here.
        for scaling_mode in (
            "SCALING_MODE_UNSPECIFIED",
            "AUTOMATIC",
            "MANUAL",
        ):
            document = minimal_runtime_service()
            document["scaling"] = {"scalingMode": scaling_mode}
            with self.subTest(valid_scaling_mode=scaling_mode):
                self.assertEqual(
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )["classification"],
                    "RUNTIME_CANONICAL",
                )

        self.assert_validation_code(
            "RUNTIME_DRIFT",
            validator.validate_runtime_comparison,
            {"pre": absent, "post": automatic},
            self.authorized_scope,
        )

        for field, old, new in (
            ("minInstanceCount", 0, 1),
            ("maxInstanceCount", 20, 21),
            ("scalingMode", "AUTOMATIC", "MANUAL"),
        ):
            pre = minimal_runtime_service()
            pre["scaling"] = {
                "scalingMode": "AUTOMATIC",
                "minInstanceCount": 0,
                "maxInstanceCount": 20,
            }
            post = json.loads(json.dumps(pre))
            post["scaling"][field] = new
            self.assertEqual(pre["scaling"][field], old)
            with self.subTest(service_scaling_drift=field):
                self.assert_validation_code(
                    "RUNTIME_DRIFT",
                    validator.validate_runtime_comparison,
                    {"pre": pre, "post": post},
                    self.authorized_scope,
                )

        malformed = (
            ("null scaling", None),
            ("array scaling", []),
            ("empty scaling", {}),
            ("negative count", {"minInstanceCount": -1}),
            ("int32 overflow", {"maxInstanceCount": 2_147_483_648}),
            ("contradictory bounds", {"minInstanceCount": 2, "maxInstanceCount": 1}),
            ("boolean count", {"manualInstanceCount": True}),
            ("float count", {"manualInstanceCount": 1.0}),
            ("numeric string count", {"manualInstanceCount": "1"}),
            ("null count", {"manualInstanceCount": None}),
            ("object count", {"manualInstanceCount": {}}),
            ("array count", {"manualInstanceCount": []}),
            ("unknown key", {"unexpected": 1}),
        )
        for label, scaling in malformed:
            document = minimal_runtime_service()
            document["scaling"] = scaling
            with self.subTest(invalid_service_scaling=label):
                self.assert_validation_code(
                    "RUNTIME_SERVICE_SCALING",
                    validator.validate_runtime_service_document,
                    document,
                    self.authorized_scope,
                )

        for scaling_mode in (
            "automatic",
            " AUTOMATIC",
            "AUTOMATIC ",
            "X_AUTOMATIC",
            "AUTOMATIC_X",
            "AUTO",
            "UNKNOWN",
            "",
        ):
            document = minimal_runtime_service()
            document["scaling"] = {"scalingMode": scaling_mode}
            with self.subTest(invalid_scaling_mode=repr(scaling_mode)):
                self.assert_validation_code(
                    "RUNTIME_SERVICE_SCALING",
                    validator.validate_runtime_service_document,
                    document,
                    self.authorized_scope,
                )

    def test_readiness_probe_valid_actions_empty_actions_and_presence(self):
        absent = minimal_runtime_service()
        valid_actions = (
            ("http populated", {"httpGet": {"path": "/ready", "port": 8080}}),
            ("tcp populated", {"tcpSocket": {"port": 8080}}),
            ("grpc populated", {"grpc": {"port": 8080, "service": "ready"}}),
            ("http header name", {"httpGet": {"httpHeaders": [{"name": "X-Probe"}]}}),
            ("http empty action", {"httpGet": {}}),
            ("tcp empty action", {"tcpSocket": {}}),
            ("grpc empty action", {"grpc": {}}),
        )
        for label, probe in valid_actions:
            document = minimal_runtime_service()
            document["template"]["containers"][0]["readinessProbe"] = probe
            with self.subTest(valid_readiness=label):
                self.assertEqual(
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )["classification"],
                    "RUNTIME_CANONICAL",
                )

        present = minimal_runtime_service()
        present["template"]["containers"][0]["readinessProbe"] = {"httpGet": {}}
        self.assert_validation_code(
            "RUNTIME_DRIFT",
            validator.validate_runtime_comparison,
            {"pre": absent, "post": present},
            self.authorized_scope,
        )

    def test_http_readiness_probe_header_name_change_creates_drift(self):
        pre = minimal_runtime_service()
        pre["template"]["containers"][0]["readinessProbe"] = {
            "httpGet": {"httpHeaders": [{"name": "X-Probe-A"}]}
        }
        post = json.loads(json.dumps(pre))
        post["template"]["containers"][0]["readinessProbe"]["httpGet"][
            "httpHeaders"
        ][0]["name"] = "X-Probe-B"

        for document in (pre, post):
            header = document["template"]["containers"][0]["readinessProbe"][
                "httpGet"
            ]["httpHeaders"][0]
            self.assertEqual(set(header), {"name"})
            self.assertEqual(
                validator.validate_runtime_service_document(
                    document, self.authorized_scope
                )["classification"],
                "RUNTIME_CANONICAL",
            )

        post_with_original_header = json.loads(json.dumps(post))
        post_with_original_header["template"]["containers"][0]["readinessProbe"][
            "httpGet"
        ]["httpHeaders"][0]["name"] = "X-Probe-A"
        self.assertEqual(pre, post_with_original_header)
        self.assert_validation_code(
            "RUNTIME_DRIFT",
            validator.validate_runtime_comparison,
            {"pre": pre, "post": post},
            self.authorized_scope,
        )

    def test_readiness_probe_action_fields_and_timing_create_drift(self):
        drift_cases = (
            ("http path", {"httpGet": {"path": "/a", "port": 8080}}, ("httpGet", "path", "/b")),
            ("http port", {"httpGet": {"path": "/a", "port": 8080}}, ("httpGet", "port", 8081)),
            ("tcp port", {"tcpSocket": {"port": 8080}}, ("tcpSocket", "port", 8081)),
            ("grpc port", {"grpc": {"port": 8080, "service": "a"}}, ("grpc", "port", 8081)),
            ("grpc service", {"grpc": {"port": 8080, "service": "a"}}, ("grpc", "service", "b")),
        )
        for label, probe, (action, field, changed_value) in drift_cases:
            pre = minimal_runtime_service()
            pre["template"]["containers"][0]["readinessProbe"] = probe
            post = json.loads(json.dumps(pre))
            post["template"]["containers"][0]["readinessProbe"][action][field] = changed_value
            with self.subTest(readiness_action_drift=label):
                self.assert_validation_code(
                    "RUNTIME_DRIFT",
                    validator.validate_runtime_comparison,
                    {"pre": pre, "post": post},
                    self.authorized_scope,
                )

        pre = minimal_runtime_service()
        pre["template"]["containers"][0]["readinessProbe"] = {"httpGet": {}}
        post = json.loads(json.dumps(pre))
        post["template"]["containers"][0]["readinessProbe"] = {"tcpSocket": {}}
        self.assert_validation_code(
            "RUNTIME_DRIFT",
            validator.validate_runtime_comparison,
            {"pre": pre, "post": post},
            self.authorized_scope,
        )

        for field, old, new in (
            ("initialDelaySeconds", 0, 1),
            ("timeoutSeconds", 1, 2),
            ("periodSeconds", 10, 11),
            ("failureThreshold", 3, 4),
        ):
            pre = minimal_runtime_service()
            pre["template"]["containers"][0]["readinessProbe"] = {
                "httpGet": {},
                "initialDelaySeconds": 0,
                "timeoutSeconds": 1,
                "periodSeconds": 10,
                "failureThreshold": 3,
            }
            post = json.loads(json.dumps(pre))
            post["template"]["containers"][0]["readinessProbe"][field] = new
            self.assertEqual(pre["template"]["containers"][0]["readinessProbe"][field], old)
            with self.subTest(readiness_timing_drift=field):
                self.assert_validation_code(
                    "RUNTIME_DRIFT",
                    validator.validate_runtime_comparison,
                    {"pre": pre, "post": post},
                    self.authorized_scope,
                )

    def test_readiness_probe_timing_boundaries_and_invalid_types(self):
        valid_boundaries = (
            ("initial minimum", {"initialDelaySeconds": 0}),
            ("initial int32 maximum", {"initialDelaySeconds": 2_147_483_647}),
            ("timeout minimum", {"timeoutSeconds": 1}),
            ("timeout maximum", {"timeoutSeconds": 3600}),
            ("period minimum", {"periodSeconds": 1}),
            ("period int32 maximum", {"periodSeconds": 2_147_483_647}),
            ("failure minimum", {"failureThreshold": 1}),
            ("failure int32 maximum", {"failureThreshold": 2_147_483_647}),
            ("timeout equals period", {"timeoutSeconds": 10, "periodSeconds": 10}),
        )
        for label, timing in valid_boundaries:
            document = minimal_runtime_service()
            document["template"]["containers"][0]["readinessProbe"] = {
                "httpGet": {},
                **timing,
            }
            with self.subTest(valid_readiness_boundary=label):
                self.assertEqual(
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )["classification"],
                    "RUNTIME_CANONICAL",
                )

        invalid_timing = []
        for value in (-1, True, "1", None, {}, []):
            invalid_timing.append(("initialDelaySeconds", value))
        for field in ("timeoutSeconds", "periodSeconds", "failureThreshold"):
            for value in (0, -1, True, "1", None, {}, []):
                invalid_timing.append((field, value))
        invalid_timing.extend(
            (
                ("initialDelaySeconds", 2_147_483_648),
                ("periodSeconds", 2_147_483_648),
                ("timeoutSeconds", 3601),
                ("failureThreshold", 2_147_483_648),
            )
        )
        for field, value in invalid_timing:
            document = minimal_runtime_service()
            document["template"]["containers"][0]["readinessProbe"] = {
                "httpGet": {},
                field: value,
            }
            with self.subTest(invalid_readiness_timing=field, value=repr(value)):
                self.assert_validation_code(
                    "RUNTIME_PROBE",
                    validator.validate_runtime_service_document,
                    document,
                    self.authorized_scope,
                )

        document = minimal_runtime_service()
        document["template"]["containers"][0]["readinessProbe"] = {
            "httpGet": {},
            "timeoutSeconds": 11,
            "periodSeconds": 10,
        }
        self.assert_validation_code(
            "RUNTIME_PROBE",
            validator.validate_runtime_service_document,
            document,
            self.authorized_scope,
        )

    def test_readiness_probe_invalid_structures_fail_for_probe_reason(self):
        malformed = (
            ("null probe", None),
            ("empty probe", {}),
            ("no action", {"periodSeconds": 10}),
            ("multiple actions", {"httpGet": {}, "tcpSocket": {}}),
            ("unsupported action", {"exec": {}}),
            ("unknown probe field", {"httpGet": {}, "successThreshold": 1}),
            ("null action", {"httpGet": None}),
            ("invalid HTTP path", {"httpGet": {"path": True}}),
            ("invalid HTTP port", {"httpGet": {"port": 0}}),
            ("invalid TCP port", {"tcpSocket": {"port": 0}}),
            ("invalid gRPC port", {"grpc": {"port": 65536}}),
            ("invalid gRPC service", {"grpc": {"service": ""}}),
            ("invalid HTTP action field", {"httpGet": {"method": "GET"}}),
            ("invalid TCP action field", {"tcpSocket": {"host": "localhost"}}),
            ("invalid gRPC action field", {"grpc": {"authority": "localhost"}}),
            ("header values are excluded", {"httpGet": {"httpHeaders": [{"name": "X-Probe", "value": "secret"}]}}),
            ("invalid header name", {"httpGet": {"httpHeaders": [{"name": True}]}}),
        )
        for label, probe in malformed:
            document = minimal_runtime_service()
            document["template"]["containers"][0]["readinessProbe"] = probe
            with self.subTest(invalid_readiness=label):
                self.assert_validation_code(
                    "RUNTIME_PROBE",
                    validator.validate_runtime_service_document,
                    document,
                    self.authorized_scope,
                )

    def test_impossible_runtime_evidence_is_rejected_before_comparison(self):
        invalid_documents = []

        two_ports = minimal_runtime_service()
        two_ports["template"]["containers"][0]["ports"] = [
            {"name": "http1", "containerPort": 8080},
            {"name": "h2c", "containerPort": 8081},
        ]
        invalid_documents.append(("two ports", two_ports, "RUNTIME_PORT"))

        for scaling in (
            {"cpuUtilization": 0.05},
            {"cpuUtilization": 0.9000001},
            {"concurrencyUtilization": 0.9500001},
            {"cpuUtilization": 0.0, "concurrencyUtilization": 0.0},
        ):
            document = minimal_runtime_service()
            document["template"]["scaling"] = scaling
            invalid_documents.append((repr(scaling), document, "RUNTIME_SCALING"))

        for interfaces in (
            [],
            "network",
            [{"network": "default"}, {"subnetwork": "default"}],
            [{}],
            [{"network": 1}],
            [{"network": "default", "tags": "tag"}],
        ):
            document = minimal_runtime_service()
            document["template"]["vpcAccess"] = {
                "networkInterfaces": interfaces
            }
            invalid_documents.append((repr(interfaces), document, "RUNTIME_VPC"))

        for label, document, code in invalid_documents:
            with self.subTest(invalid_runtime=label):
                self.assert_validation_code(
                    code,
                    validator.validate_runtime_comparison,
                    {"pre": document, "post": document},
                    self.authorized_scope,
                )

    def test_runtime_utilization_boundaries_and_valid_equality(self):
        for scaling in (
            {"cpuUtilization": 0.0},
            {"cpuUtilization": 0.1},
            {"cpuUtilization": 0.90},
            {"concurrencyUtilization": 0.0},
            {"concurrencyUtilization": 0.1},
            {"concurrencyUtilization": 0.95},
            {"cpuUtilization": 0.90, "concurrencyUtilization": 0.95},
        ):
            document = minimal_runtime_service()
            document["template"]["scaling"] = scaling
            with self.subTest(scaling=scaling):
                self.assertEqual(
                    validator.validate_runtime_comparison(
                        {"pre": document, "post": document}, self.authorized_scope
                    )["classification"],
                    "RUNTIME_UNCHANGED",
                )

    def test_ingress_none_is_exact_and_drift_sensitive(self):
        # GoogleCloudRunV2Service.IngressValueValuesEnum in the locally
        # generated Run v2 messages, enumerated independently here.
        ingress_values = (
            "INGRESS_TRAFFIC_UNSPECIFIED",
            "INGRESS_TRAFFIC_ALL",
            "INGRESS_TRAFFIC_INTERNAL_ONLY",
            "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            "INGRESS_TRAFFIC_NONE",
        )
        for ingress in ingress_values:
            document = minimal_runtime_service()
            document["ingress"] = ingress
            with self.subTest(valid_ingress=ingress):
                self.assertEqual(
                    validator.validate_runtime_service_document(
                        document, self.authorized_scope
                    )["classification"],
                    "RUNTIME_CANONICAL",
                )

        for ingress in (
            "ingress_traffic_none",
            " INGRESS_TRAFFIC_NONE",
            "INGRESS_TRAFFIC_NONE ",
            "X_INGRESS_TRAFFIC_NONE",
            "INGRESS_TRAFFIC_NONE_X",
            "INGRESS_TRAFFIC",
            "NONE",
            "INGRESS_TRAFFIC_UNKNOWN",
            "",
        ):
            document = minimal_runtime_service()
            document["ingress"] = ingress
            with self.subTest(invalid_ingress=repr(ingress)):
                self.assert_validation_code(
                    "RUNTIME_SERVICE",
                    validator.validate_runtime_service_document,
                    document,
                    self.authorized_scope,
                )

        absent = minimal_runtime_service()
        none = minimal_runtime_service()
        none["ingress"] = "INGRESS_TRAFFIC_NONE"
        all_ingress = minimal_runtime_service()
        all_ingress["ingress"] = "INGRESS_TRAFFIC_ALL"
        for label, pre, post in (
            ("absent to none", absent, none),
            ("all to none", all_ingress, none),
            ("none to all", none, all_ingress),
        ):
            with self.subTest(ingress_drift=label):
                self.assert_validation_code(
                    "RUNTIME_DRIFT",
                    validator.validate_runtime_comparison,
                    {"pre": pre, "post": post},
                    self.authorized_scope,
                )

    def test_exact_artifact_registry_get_contract(self):
        request = validator.validate_artifact_image_request(
            expected_image_tag=IMAGE_TAG,
            expected_digest=DIGEST,
            scope=self.authorized_scope,
        )
        self.assertEqual(request["resource"], DOCKER_IMAGE_RESOURCE)
        self.assertEqual(
            request["url"],
            f"https://artifactregistry.googleapis.com/v1/{DOCKER_IMAGE_RESOURCE}"
            "?fields=name%2Curi%2Ctags",
        )
        self.assertNotIn("page", request["url"].lower())
        response = {
            "name": DOCKER_IMAGE_RESOURCE,
            "uri": IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST,
            "tags": [SOURCE_SHA],
        }
        self.assertEqual(
            validator.validate_tag_resolution_document(
                response,
                expected_image_tag=IMAGE_TAG,
                expected_project_number=PROJECT_NUMBER,
                scope=self.authorized_scope,
            )["digest"],
            DIGEST,
        )
        for invalid in (
            [],
            [response],
            {"dockerImages": [response]},
            {**response, "nextPageToken": "token"},
            {**response, "name": DOCKER_IMAGE_RESOURCE.replace(PROJECT, "other-project")},
            {**response, "name": DOCKER_IMAGE_RESOURCE.replace(REGION, "us-west1")},
            {**response, "name": DOCKER_IMAGE_RESOURCE.replace("/repositories/cbd/", "/repositories/other/")},
            {**response, "name": DOCKER_IMAGE_RESOURCE.replace("/dockerImages/", "/packages/")},
            {**response, "uri": response["uri"].replace(f"{REGION}-", "us-west1-")},
            {**response, "tags": [IMAGE_TAG.replace("/cbd/", "/other/")]},
            {**response, "tags": []},
            {**response, "tags": [IMAGE_TAG, IMAGE_TAG]},
            {**response, "uri": IMAGE_TAG.rsplit(":", 1)[0] + "@@" + DIGEST},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(validator.ValidationError):
                    validator.validate_tag_resolution_document(
                        invalid,
                        expected_image_tag=IMAGE_TAG,
                        expected_project_number=PROJECT_NUMBER,
                        scope=self.authorized_scope,
                    )

    def test_artifact_request_cli_rejects_noncanonical_scope_and_digest(self):
        args = [
            "artifact-image-request", "--project", PROJECT, "--region", REGION,
            "--service", SERVICE, "--expected-image-tag", IMAGE_TAG,
            "--expected-digest", DIGEST, "--output", "resource",
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = validator.main(args)
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), DOCKER_IMAGE_RESOURCE + "\n")
        for index, value in ((args.index(DIGEST), "prefix@" + DIGEST),):
            changed = list(args)
            changed[index] = value
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(changed)
            self.assertEqual(status, 2)
            self.assertEqual(stdout.getvalue(), "")

    def test_push_timing_is_bounded_by_build_interval_at_nanoseconds(self):
        def with_timing(start, end):
            document = json.loads(json.dumps(self.build))
            document["results"]["images"][0]["pushTiming"] = {
                "startTime": start,
                "endTime": end,
            }
            return document

        allowed = (
            ("2026-08-11T12:00:01Z", "2026-08-11T12:01:00Z"),
            ("2026-08-11T07:00:01-05:00", "2026-08-11T07:01:00-05:00"),
            ("2026-08-11T12:00:01.000000001Z", "2026-08-11T12:00:59.999999999Z"),
        )
        for start, end in allowed:
            with self.subTest(start=start, end=end):
                self.assertEqual(
                    self.validate_build(with_timing(start, end))["classification"],
                    "BUILD_SUCCESS",
                )
        rejected = (
            ("2026-08-11T12:00:00.999999999Z", "2026-08-11T12:00:02Z"),
            ("2026-08-11T12:00:02Z", "2026-08-11T12:01:00.000000001Z"),
            ("2026-08-11T12:00:02.000000001Z", "2026-08-11T12:00:02Z"),
        )
        for start, end in rejected:
            with self.subTest(start=start, end=end):
                self.assert_validation_code(
                    "BUILT_IMAGE_PUSH_TIMING_ORDER",
                    self.validate_build,
                    with_timing(start, end),
                )

    def test_revision_digest_requires_exact_bound_image_reference(self):
        self.assertEqual(
            validate_revision(revision_document())["digest"],
            DIGEST,
        )
        invalid = (
            (DIGEST, "REVISION_IMAGE"),
            (f"docker.io/example/image@{DIGEST}", "REVISION_IMAGE_IDENTITY"),
            (
                f"{REGION}-docker.pkg.dev/other-project/cbd/cbd-assess@{DIGEST}",
                "REVISION_IMAGE_IDENTITY",
            ),
            (
                f"{REGION}-docker.pkg.dev/{PROJECT}/other/cbd-assess@{DIGEST}",
                "REVISION_IMAGE_IDENTITY_MISMATCH",
            ),
            (
                f"{REGION}-docker.pkg.dev/{PROJECT}/cbd/other@{DIGEST}",
                "REVISION_IMAGE_IDENTITY_MISMATCH",
            ),
            (IMAGE_TAG, "REVISION_IMAGE"),
            (IMAGE_URI + DIGEST, "REVISION_IMAGE"),
            (IMAGE_URI + "@@" + DIGEST, "REVISION_IMAGE"),
            (IMAGE_URI + "@sha256:" + "a" * 63, "REVISION_DIGEST"),
            (IMAGE_URI + "@" + DIGEST.upper(), "REVISION_DIGEST"),
            (None, "REVISION_IMAGE"),
        )
        for value, code in invalid:
            with self.subTest(value=repr(value), code=code):
                self.assert_validation_code(
                    code,
                    validate_revision,
                    revision_document(digest=value),
                )
        self.assert_validation_code(
            "REVISION_DIGEST_MISMATCH",
            validate_revision,
            revision_document(digest=f"{IMAGE_URI}@{OTHER_DIGEST}"),
        )
        missing = revision_document()
        del missing["status"]["imageDigest"]
        with self.assertRaises(validator.ValidationError):
            validate_revision(missing)

    def test_revision_expected_digest_and_cli_failure_are_canonical_and_sanitized(self):
        invalid_expected = (
            "malformed-prefix@" + DIGEST,
            "one@two@" + DIGEST,
            IMAGE_TAG,
            " " + DIGEST,
            DIGEST + "\n",
            DIGEST.upper(),
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
        )
        for value in invalid_expected:
            with self.subTest(value=repr(value)):
                self.assert_validation_code(
                    "EXPECTED_DIGEST",
                    validate_revision,
                    revision_document(),
                    BASELINE,
                    value,
                )
        unsafe = "UNSAFE_VALUE_MUST_NOT_BE_ECHOED"
        status, stdout, stderr = Phase2FCliTests().invoke(
            [
                "revision", "--project", PROJECT, "--region", REGION,
                "--service", SERVICE, "--expected-revision", BASELINE,
                "--expected-digest", DIGEST, "--expected-image", IMAGE_URI,
            ],
            scoped("revision", revision_document(digest=unsafe)),
        )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn(unsafe, stderr)

    def test_revision_expected_image_is_exact_tagless_digestless_base_uri(self):
        self.assertEqual(
            validate_revision(revision_document(), expected_image=IMAGE_URI)["digest"],
            DIGEST,
        )
        invalid_expected_images = (
            ("tag qualified", IMAGE_URI + ":candidate"),
            ("digest qualified", IMAGE_URI + "@" + DIGEST),
            ("leading whitespace", " " + IMAGE_URI),
            ("trailing whitespace", IMAGE_URI + " "),
            ("malformed registry", f"{REGION}.docker.pkg.dev/{PROJECT}/cbd/cbd-assess"),
            ("malformed path", f"{REGION}-docker.pkg.dev/{PROJECT}//cbd-assess"),
            ("extra path", IMAGE_URI + "/extra"),
            ("missing project", f"{REGION}-docker.pkg.dev/cbd/cbd-assess"),
            ("missing repository", f"{REGION}-docker.pkg.dev/{PROJECT}/cbd-assess"),
            ("missing image", f"{REGION}-docker.pkg.dev/{PROJECT}/cbd"),
            ("query suffix", IMAGE_URI + "?tag=candidate"),
            ("fragment suffix", IMAGE_URI + "#candidate"),
        )
        for label, expected_image in invalid_expected_images:
            with self.subTest(invalid_expected_image=label):
                self.assert_validation_code(
                    "EXPECTED_IMAGE",
                    validate_revision,
                    revision_document(),
                    BASELINE,
                    DIGEST,
                    expected_image,
                )

        status, stdout, stderr = Phase2FCliTests().invoke(
            [
                "revision", "--project", PROJECT, "--region", REGION,
                "--service", SERVICE, "--expected-revision", BASELINE,
                "--expected-digest", DIGEST, "--expected-image", IMAGE_TAG,
            ],
            scoped("revision", revision_document()),
        )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("EXPECTED_IMAGE", stderr)

    def test_revision_evidence_wrong_image_identity_keeps_expected_image_valid(self):
        wrong_evidence = (
            ("wrong host", f"docker.io/example/cbd-assess@{DIGEST}", "REVISION_IMAGE_IDENTITY"),
            ("wrong region", f"us-west1-docker.pkg.dev/{PROJECT}/cbd/cbd-assess@{DIGEST}", "REVISION_IMAGE_IDENTITY"),
            ("wrong project", f"{REGION}-docker.pkg.dev/other-project/cbd/cbd-assess@{DIGEST}", "REVISION_IMAGE_IDENTITY"),
            ("wrong repository", f"{REGION}-docker.pkg.dev/{PROJECT}/other/cbd-assess@{DIGEST}", "REVISION_IMAGE_IDENTITY_MISMATCH"),
            ("wrong image", f"{REGION}-docker.pkg.dev/{PROJECT}/cbd/other@{DIGEST}", "REVISION_IMAGE_IDENTITY_MISMATCH"),
        )
        for label, observed_image, code in wrong_evidence:
            with self.subTest(wrong_revision_image=label):
                self.assert_validation_code(
                    code,
                    validate_revision,
                    revision_document(digest=observed_image),
                )

    def test_documentation_matches_corrected_contracts(self):
        runbook = Path("DEPLOYMENT_RUNBOOK.md").read_text(encoding="utf-8")
        readme = Path("README.md").read_text(encoding="utf-8")
        handoff = Path("HANDOFF.md").read_text(encoding="utf-8")
        checklist = Path("MANUAL_TEST_CHECKLIST.md").read_text(encoding="utf-8")
        self.assertNotIn("gcloud artifacts docker images describe", runbook)
        self.assertIn("artifact-image-request", runbook)
        self.assertIn("DockerImages.Get", runbook)
        self.assertIn("secret-reference-result", runbook)
        self.assertIn("json(projectId,projectNumber,lifecycleState)", runbook)
        self.assertIn('--project-number="$PROJECT_NUMBER"', runbook)
        for document in (runbook, readme, handoff, checklist):
            normalized = " ".join(document.split())
            self.assertIn("project ID", normalized)
            self.assertIn("project number", normalized)
            self.assertIn("observed resource identity", normalized)
        self.assertIn("pushStart <= pushEnd <= finishTime", runbook)
        for document in (runbook, readme, handoff, checklist):
            normalized = " ".join(document.split())
            self.assertIn("A singleton container may omit `name`", normalized)
            self.assertIn(
                "Multiple containers require explicit, valid, unique names",
                normalized,
            )
            self.assertIn("compared by name independent of order", normalized)
        self.assertNotIn("nonempty named container", runbook)
        self.assertIn("--expected-image=", runbook)
        self.assertIn("tagless, digestless base", runbook)
        self.assertIn("Artifact Registry image URI", runbook)
        self.assertIn("rejected rather than stripped or normalized", runbook)
        self.assertIn("scaling/manualInstanceCount", runbook)
        self.assertIn("template/containers/readinessProbe", runbook)
        self.assertNotIn("template/containers/env/value,", runbook)
        self.assertIn("non-paginated", readme)
        self.assertIn("historical Phase 2B observation", handoff)
        self.assertNotIn("current verified service state", handoff)
        self.assertIn("service-level and revision-level", handoff)
        self.assertIn("readiness probes", handoff)
        self.assertIn("schema-validated safe runtime projection", checklist)
        self.assertIn("INGRESS_TRAFFIC_NONE", checklist)


class AuthoritativeBuildEvidenceTests(ValidatorTestCase):
    def setUp(self):
        self.scope = validator.require_scope(PROJECT, REGION, SERVICE)
        self.build = Phase2FGateTests().build_document()
        self.config = explicit_build_config()

    def validate(self, document=None, **overrides):
        kwargs = build_validation_kwargs()
        kwargs.update(overrides)
        return validator.validate_build_document(
            self.build if document is None else document,
            expected_build_id=BUILD_ID,
            expected_source_sha=SOURCE_SHA,
            expected_source_tree=SOURCE_TREE,
            expected_image_tag=IMAGE_TAG,
            scope=self.scope,
            **kwargs,
        )

    def test_authoritative_resolved_build_envelope_passes(self):
        result = self.validate()
        self.assertEqual(result["classification"], "BUILD_SUCCESS")
        self.assertEqual(result["imageDigest"], DIGEST)

    def test_verified_numeric_build_project_alias_passes(self):
        document = json.loads(json.dumps(self.build))
        document["name"] = document["name"].replace(
            f"projects/{PROJECT}/", f"projects/{PROJECT_NUMBER}/"
        )
        self.assertEqual(self.validate(document)["buildResource"], document["name"])

    def test_wrong_textual_or_numeric_build_project_fails(self):
        for project in ("other-project", "999999999999"):
            document = json.loads(json.dumps(self.build))
            document["name"] = document["name"].replace(
                f"projects/{PROJECT}/", f"projects/{project}/"
            )
            with self.subTest(project=project), self.assertRaises(validator.ValidationError):
                self.validate(document)

    def test_returned_placeholders_and_wrong_resolved_arguments_fail(self):
        replacements = ("${_CANDIDATE_IMAGE}", "c" * 40, "d" * 40, IMAGE_TAG + "-other")
        indexes = (2, 4, 6, 2)
        for replacement, index in zip(replacements, indexes):
            document = json.loads(json.dumps(self.build))
            document["steps"][0]["args"][index] = replacement
            with self.subTest(value=replacement), self.assertRaises(validator.ValidationError):
                self.validate(document)

    def test_returned_image_inventory_is_exact(self):
        for images in ([IMAGE_TAG + "-other"], [IMAGE_TAG, IMAGE_TAG + "-other"], []):
            document = json.loads(json.dumps(self.build))
            document["images"] = images
            with self.subTest(images=images), self.assertRaises(validator.ValidationError):
                self.validate(document)

    def test_returned_must_match_explicit_or_default_omission(self):
        self.assertEqual(self.validate()["classification"], "BUILD_SUCCESS")
        document = json.loads(json.dumps(self.build))
        document["options"] = {}
        self.assertEqual(self.validate(document)["classification"], "BUILD_SUCCESS")
        with self.assertRaises(validator.ValidationError):
            self.validate(document, submitted_config=None)

    def test_returned_allow_loose_and_unexpected_option_fail(self):
        for options in ({"substitutionOption": "ALLOW_LOOSE"}, {"machineType": "E2_HIGHCPU_8"}):
            document = json.loads(json.dumps(self.build))
            document["options"] = options
            with self.subTest(options=options), self.assertRaises(validator.ValidationError):
                self.validate(document)

    def test_source_and_resolved_source_must_match_exactly(self):
        document = json.loads(json.dumps(self.build))
        document["sourceProvenance"]["resolvedStorageSource"]["generation"] = "2"
        with self.assertRaises(validator.ValidationError):
            self.validate(document)

    def test_exact_package_version_and_digest_binding(self):
        expected = PACKAGE_RESOURCE + "/versions/" + DIGEST
        self.assertEqual(self.validate()["packageVersionResource"], expected)
        for package in (PACKAGE_RESOURCE, PACKAGE_RESOURCE + "/versions/" + OTHER_DIGEST):
            document = json.loads(json.dumps(self.build))
            document["results"]["images"][0]["artifactRegistryPackage"] = package
            with self.subTest(package=package), self.assertRaises(validator.ValidationError):
                self.validate(document)

    def test_bare_artifact_registry_tag_is_exact_and_unambiguous(self):
        evidence = {
            "name": DOCKER_IMAGE_RESOURCE,
            "uri": IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST,
            "tags": [SOURCE_SHA],
        }
        result = validator.validate_tag_resolution_document(
            evidence, expected_image_tag=IMAGE_TAG,
            expected_project_number=PROJECT_NUMBER, scope=self.scope,
        )
        self.assertEqual(result["classification"], "TAG_RESOLVED")
        for tags in (["c" * 40], [SOURCE_SHA, "c" * 40], [IMAGE_TAG]):
            with self.subTest(tags=tags), self.assertRaises(validator.ValidationError):
                validator.validate_tag_resolution_document(
                    {**evidence, "tags": tags}, expected_image_tag=IMAGE_TAG,
                    expected_project_number=PROJECT_NUMBER, scope=self.scope,
                )

    def test_numeric_docker_image_project_alias_is_verified(self):
        evidence = {
            "name": DOCKER_IMAGE_RESOURCE.replace(
                f"projects/{PROJECT}/", f"projects/{PROJECT_NUMBER}/"
            ),
            "uri": IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST,
            "tags": [SOURCE_SHA],
        }
        self.assertEqual(
            validator.validate_tag_resolution_document(
                evidence, expected_image_tag=IMAGE_TAG,
                expected_project_number=PROJECT_NUMBER, scope=self.scope,
            )["digest"], DIGEST,
        )

    def test_unexpected_returned_step_substitution_and_field_fail(self):
        mutations = []
        extra_step = json.loads(json.dumps(self.build))
        extra_step["steps"].append(extra_step["steps"][0])
        mutations.append(extra_step)
        extra_substitution = json.loads(json.dumps(self.build))
        extra_substitution["substitutions"]["_EXTRA"] = "value"
        mutations.append(extra_substitution)
        extra_field = json.loads(json.dumps(self.build))
        extra_field["unexpected"] = True
        mutations.append(extra_field)
        for document in mutations:
            with self.subTest(keys=sorted(document)), self.assertRaises(validator.ValidationError):
                self.validate(document)


class Phase2FCliTests(unittest.TestCase):
    common = ["--project", PROJECT, "--region", REGION, "--service", SERVICE]

    def invoke(self, args, document):
        with tempfile.TemporaryDirectory(prefix="phase2f-cli-config.") as created_root:
            effective_args = list(args)
            if effective_args[0] == "build":
                root = __import__("os").path.realpath(created_root)
                config_file = Path(root) / "cloudbuild.json"
                config_bytes = json.dumps(explicit_build_config()).encode("utf-8")
                config_file.write_bytes(config_bytes)
                effective_args.extend([
                    "--project-number", PROJECT_NUMBER,
                    "--expected-service-account", BUILD_SERVICE_ACCOUNT,
                    "--evidence-root", root,
                    "--build-config-file", str(config_file),
                    "--expected-build-config-sha256", hashlib.sha256(config_bytes).hexdigest(),
                ])
            elif effective_args[0] == "tag-resolution":
                effective_args.extend(["--project-number", PROJECT_NUMBER])
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(validator.sys, "stdin", io.StringIO(json.dumps(document))):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = validator.main(effective_args)
            return status, stdout.getvalue(), stderr.getvalue()

    def valid_commands(self):
        pre = traffic_document(fixed(BASELINE, 100))
        post = traffic_document(fixed(BASELINE, 100), latest_created=CANDIDATE)
        build = Phase2FGateTests().build_document()
        docker_image = {
            "name": DOCKER_IMAGE_RESOURCE,
            "uri": IMAGE_TAG.rsplit(":", 1)[0] + "@" + DIGEST,
            "tags": [SOURCE_SHA],
        }
        return [
            (
                [
                    "project-identity",
                    *self.common,
                    "--project-number",
                    PROJECT_NUMBER,
                ],
                {
                    "projectId": PROJECT,
                    "projectNumber": PROJECT_NUMBER,
                    "lifecycleState": "ACTIVE",
                },
            ),
            (
                [
                    "revision", *self.common, "--expected-revision", BASELINE,
                    "--expected-digest", DIGEST, "--expected-image", IMAGE_URI,
                ],
                scoped("revision", revision_document()),
            ),
            (
                ["traffic", *self.common, "--latest-ready-revision", BASELINE],
                scoped("serviceState", pre),
            ),
            (
                [
                    "zero-traffic",
                    *self.common,
                    "--candidate-revision",
                    CANDIDATE,
                    "--baseline-revision",
                    BASELINE,
                    "--pre-latest-ready-revision",
                    BASELINE,
                ],
                scoped("transition", {"pre": pre, "post": post}),
            ),
            (["session-secret", *self.common, "--project-number", PROJECT_NUMBER], scoped("serviceConfig", session_document(session_reference()))),
            (
                ["secret-version", *self.common, "--project-number", PROJECT_NUMBER, "--expected-secret", "session-secret", "--expected-version", "7"],
                scoped("secretMetadata", secret_metadata()),
            ),
            (
                ["nonexistence", *self.common, "--kind", "CANDIDATE_TAG", "--expected-resource", TAG_RESOURCE],
                scoped("existence", {"httpStatus": 404, "body": {"error": {"code": 404, "status": "NOT_FOUND"}}}),
            ),
            (
                ["build", *self.common, "--expected-build-id", BUILD_ID, "--expected-source-sha", SOURCE_SHA, "--expected-source-tree", SOURCE_TREE, "--expected-image-tag", IMAGE_TAG],
                scoped("build", build),
            ),
            (
                ["tag-resolution", *self.common, "--expected-image-tag", IMAGE_TAG],
                scoped("tag", docker_image),
            ),
            (
                ["runtime-snapshot", *self.common],
                scoped("serviceConfig", minimal_runtime_service()),
            ),
            (
                ["runtime-equal", *self.common],
                scoped(
                    "comparison",
                    {"pre": minimal_runtime_service(), "post": minimal_runtime_service()},
                ),
            ),
            (
                ["traffic-map", *self.common, "--purpose", "TRAFFIC"],
                scoped("comparison", {"observed": post, "expected": post}),
            ),
        ]

    def test_every_cli_subcommand_has_deterministic_success_and_safe_failure(self):
        sentinel = "ADVERSARIAL_RAW_EVIDENCE_MUST_NOT_APPEAR"
        for args, document in self.valid_commands():
            with self.subTest(command=args[0]):
                first = self.invoke(args, document)
                second = self.invoke(args, document)
                self.assertEqual(first, second)
                self.assertEqual(first[0], 0)
                self.assertEqual(first[2], "")
                self.assertNotIn(sentinel, first[1])
                failed = self.invoke(args, {"unexpected": sentinel})
                self.assertEqual(failed[0], 2)
                self.assertEqual(failed[1], "")
                self.assertNotIn(sentinel, failed[2])
                self.assertNotIn("Traceback", failed[2])

    def test_zero_traffic_binds_nonbaseline_ready_revision_to_evidence_file(self):
        pre_ready_candidate = "cbd-assess-17237de"
        with tempfile.TemporaryDirectory(prefix="phase2f-pre-ready.") as root:
            root = __import__("os").path.realpath(root)
            pre_path = Path(root) / "pre.json"
            post_path = Path(root) / "post.json"
            revision_path = Path(root) / "approved-revision.json"
            pre_path.write_text(json.dumps(traffic_document(
                fixed(BASELINE, 100), latest_ready=pre_ready_candidate
            )))
            post_path.write_text(json.dumps(traffic_document(
                fixed(BASELINE, 100), latest_created=CANDIDATE
            )))
            revision_path.write_text(json.dumps(revision_document(
                name=pre_ready_candidate
            )))
            args = [
                "zero-traffic", *self.common,
                "--candidate-revision", CANDIDATE,
                "--baseline-revision", BASELINE,
                "--pre-latest-ready-revision", pre_ready_candidate,
                "--evidence-root", root,
                "--pre-evidence-file", str(pre_path),
                "--post-evidence-file", str(post_path),
                "--pre-approved-latest-ready-evidence-file", str(revision_path),
                "--pre-approved-latest-ready-digest", DIGEST,
                "--pre-approved-latest-ready-image", IMAGE_URI,
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
            self.assertEqual(status, 0)
            revision_path.write_text(json.dumps(revision_document(
                name="cbd-assess-unapproved"
            )))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = validator.main(args)
            self.assertEqual(status, 2)

    def test_build_nonterminal_exit_is_stable(self):
        args, document = next(
            item for item in self.valid_commands() if item[0][0] == "build"
        )
        document["build"]["status"] = "WORKING"
        document["build"].pop("finishTime")
        document["build"].pop("results")
        result = self.invoke(args, document)
        self.assertEqual(result, (3, '{"classification":"BUILD_NONTERMINAL"}\n', ""))

    def test_unexpected_exception_is_redacted_without_traceback(self):
        sentinel = "ADVERSARIAL_EXCEPTION_SECRET"
        args, document = self.valid_commands()[0]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(validator, "_read_stdin_document", side_effect=RuntimeError(sentinel)):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = validator.main(args)
        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(status, 2)
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn("Traceback", rendered)
        self.assertIn("INTERNAL_VALIDATION_ERROR", rendered)


if __name__ == "__main__":
    unittest.main()
