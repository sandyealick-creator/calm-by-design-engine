#!/usr/bin/env python3
"""Strict, local validation for controlled Cloud Run deployment evidence.

The validator reads JSON from standard input and writes only deterministic,
non-secret validation results. It never invokes commands, reads credentials,
contacts a network, or mutates files or resources.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, NoReturn, Sequence


DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION_RE = re.compile(r"[a-z]([a-z0-9-]*[a-z0-9])?")
PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9-]{4,28}[a-z0-9]")
PROJECT_NUMBER_RE = re.compile(r"[1-9][0-9]{5,29}")
REGION_RE = re.compile(r"[a-z][a-z0-9-]{1,30}[a-z0-9]")
SECRET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")
NUMERIC_VERSION_RE = re.compile(r"[1-9][0-9]*")
BUILD_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SECRET_RESOURCE_RE = re.compile(
    r"projects/(?P<project>[a-z0-9][a-z0-9-]{4,28}[a-z0-9])/secrets/"
    r"(?P<secret>[A-Za-z0-9_-]{1,255})"
)
VERSION_RESOURCE_RE = re.compile(
    r"(?P<secret_resource>projects/[a-z0-9][a-z0-9-]{4,28}[a-z0-9]/"
    r"secrets/[A-Za-z0-9_-]{1,255})/versions/(?P<version>[1-9][0-9]*)"
)
AR_COMPONENT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
IMAGE_TAG_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RFC3339_TIMESTAMP_RE = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?"
    r"(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})"
)
DURATION_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?s")
ARTIFACT_REGISTRY_API_ORIGIN = "https://artifactregistry.googleapis.com/v1"
EXPLICIT_BUILD_STEP_NAME = "gcr.io/cloud-builders/docker"
EXPLICIT_BUILD_ARGS = [
    "build",
    "--tag",
    "${_CANDIDATE_IMAGE}",
    "--label",
    "org.opencontainers.image.revision=${_SOURCE_SHA}",
    "--label",
    "com.calmbydesign.source-tree=${_SOURCE_TREE}",
    ".",
]
EXPLICIT_BUILD_IMAGES = ["${_CANDIDATE_IMAGE}"]
EXPLICIT_BUILD_OPTIONS = {"substitutionOption": "MUST_MATCH"}


class ValidationError(ValueError):
    """A fail-closed validation result with a safe, stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code: str, message: str) -> NoReturn:
    raise ValidationError(code, message)


class NonterminalBuild(ValueError):
    """A safe retry classification for a valid nonterminal build."""


@dataclass(frozen=True)
class Scope:
    project: str
    region: str
    service: str

    @property
    def service_resource(self) -> str:
        return (
            f"projects/{self.project}/locations/{self.region}/services/{self.service}"
        )

    def output(self) -> dict[str, str]:
        return {
            "project": self.project,
            "region": self.region,
            "service": self.service,
        }


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    project_number: str

    @property
    def aliases(self) -> frozenset[str]:
        return frozenset((self.project_id, self.project_number))

    def output(self) -> dict[str, str]:
        return {
            "projectId": self.project_id,
            "projectNumber": self.project_number,
        }


@dataclass(frozen=True)
class ImageIdentity:
    project: str
    region: str
    repository: str
    image: str
    tag: str

    @property
    def tagged_uri(self) -> str:
        return (
            f"{self.region}-docker.pkg.dev/{self.project}/{self.repository}/"
            f"{self.image}:{self.tag}"
        )

    @property
    def image_uri(self) -> str:
        return self.tagged_uri.rsplit(":", 1)[0]

    @property
    def package_resource(self) -> str:
        return (
            f"projects/{self.project}/locations/{self.region}/repositories/"
            f"{self.repository}/packages/{self.image}"
        )

    def docker_image_resource(self, digest: str) -> str:
        return (
            f"projects/{self.project}/locations/{self.region}/repositories/"
            f"{self.repository}/dockerImages/{self.image}@{digest}"
        )

    def digest_uri(self, digest: str) -> str:
        return f"{self.image_uri}@{digest}"


@dataclass(frozen=True, order=True)
class ExactTimestamp:
    """One protobuf Timestamp normalized without losing nanoseconds."""

    epoch_seconds: int
    nanoseconds: int


def require_scope(project: Any, region: Any, service: Any) -> Scope:
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        fail("PROJECT_ID", "Project identity is malformed")
    if not isinstance(region, str) or not REGION_RE.fullmatch(region):
        fail("REGION", "Region identity is malformed")
    service = _require_revision_name(service, "SERVICE")
    return Scope(project, region, service)


def require_project_identity(
    project_id: Any, project_number: Any
) -> ProjectIdentity:
    if (
        not isinstance(project_id, str)
        or not PROJECT_RE.fullmatch(project_id)
        or project_id.isdigit()
    ):
        fail("PROJECT_ID", "Project ID is malformed")
    if not isinstance(project_number, str) or not PROJECT_NUMBER_RE.fullmatch(
        project_number
    ):
        fail("PROJECT_NUMBER", "Project number is malformed")
    return ProjectIdentity(project_id, project_number)


def validate_project_identity_document(
    document: Any, *, expected_project_id: str, expected_project_number: str
) -> dict[str, str]:
    """Bind one active project ID to its exact authoritative project number."""

    expected = require_project_identity(expected_project_id, expected_project_number)
    root = _require_object(
        document, "PROJECT_IDENTITY", "Project metadata evidence is malformed"
    )
    _require_exact_keys(
        root,
        required={"projectId", "projectNumber", "lifecycleState"},
        code="PROJECT_IDENTITY",
    )
    observed = require_project_identity(root["projectId"], root["projectNumber"])
    if observed != expected:
        fail("PROJECT_IDENTITY_MISMATCH", "Project identity pair differs")
    if root["lifecycleState"] != "ACTIVE":
        fail("PROJECT_LIFECYCLE", "Authorized project is not active")
    return {
        "classification": "VERIFIED_ACTIVE_PROJECT_IDENTITY",
        **observed.output(),
        "lifecycleState": "ACTIVE",
    }


def scoped_payload(
    document: Any,
    payload_key: str,
    *,
    project: str,
    region: str,
    service: str,
) -> tuple[Scope, Any]:
    expected = require_scope(project, region, service)
    root = _require_object(document, "SCOPED_EVIDENCE", "Scoped evidence is malformed")
    _require_exact_keys(root, required={"scope", payload_key})
    raw_scope = _require_object(root["scope"], "SCOPE", "Evidence scope is malformed")
    _require_exact_keys(raw_scope, required={"project", "region", "service"})
    observed = require_scope(
        raw_scope["project"], raw_scope["region"], raw_scope["service"]
    )
    if observed != expected:
        fail("SCOPE_MISMATCH", "Evidence scope differs from the authorized scope")
    return expected, root[payload_key]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("DUPLICATE_JSON_KEY", "JSON contains a duplicate object key")
        result[key] = value
    return result


def strict_loads(raw: str) -> Any:
    """Parse one strict JSON document and reject duplicate object keys."""

    if not isinstance(raw, str) or not raw.strip():
        fail("EMPTY_INPUT", "JSON evidence is empty")
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ValidationError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        fail("MALFORMED_JSON", "JSON evidence is malformed or truncated")


def _require_object(value: Any, code: str, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(code, message)
    return value


def _require_exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    code: str = "UNEXPECTED_STRUCTURE",
) -> None:
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        fail(code, "JSON evidence has missing or unexpected fields")


def _require_revision_name(value: Any, code: str) -> str:
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value) or len(value) > 63:
        fail(code, "Revision identity is malformed")
    return value


def _canonical_digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        fail(code, "Image digest is not one canonical immutable digest")
    return value


def _image_identity(value: Any, scope: Scope) -> ImageIdentity:
    if not isinstance(value, str) or value != value.strip() or "@" in value:
        fail("IMAGE_TAG", "Candidate image tag is malformed")
    pattern = re.compile(
        rf"(?P<region>{REGION_RE.pattern})-docker\.pkg\.dev/"
        rf"(?P<project>{PROJECT_RE.pattern})/"
        rf"(?P<repository>{AR_COMPONENT_RE.pattern})/"
        rf"(?P<image>{AR_COMPONENT_RE.pattern}):"
        rf"(?P<tag>{IMAGE_TAG_COMPONENT_RE.pattern})"
    )
    match = pattern.fullmatch(value)
    if match is None:
        fail("IMAGE_TAG", "Candidate image tag is malformed")
    identity = ImageIdentity(**match.groupdict())
    if identity.project != scope.project or identity.region != scope.region:
        fail("IMAGE_SCOPE_MISMATCH", "Candidate image tag identifies another scope")
    return identity


def _canonical_image_uri(value: Any, scope: Scope, code: str) -> str:
    """Validate one canonical Artifact Registry image identity without tag/digest."""

    if not isinstance(value, str) or value != value.strip() or "@" in value:
        fail(code, "Revision image identity is malformed")
    pattern = re.compile(
        rf"(?P<region>{REGION_RE.pattern})-docker\.pkg\.dev/"
        rf"(?P<project>{PROJECT_RE.pattern})/"
        rf"(?P<repository>{AR_COMPONENT_RE.pattern})/"
        rf"(?P<image>{AR_COMPONENT_RE.pattern})"
    )
    match = pattern.fullmatch(value)
    if match is None:
        fail(code, "Revision image identity is malformed")
    if match.group("project") != scope.project or match.group("region") != scope.region:
        fail(code, "Revision image identity differs from the authorized scope")
    return value


def _parse_build_timestamp(value: Any, field: str) -> ExactTimestamp:
    if not isinstance(value, str):
        fail("BUILD_TIME", f"Successful build {field} is missing or malformed")
    match = RFC3339_TIMESTAMP_RE.fullmatch(value)
    if match is None:
        fail("BUILD_TIME", f"Successful build {field} is missing or malformed")
    try:
        local_time = datetime.strptime(
            f"{match.group('date')}T{match.group('time')}", "%Y-%m-%dT%H:%M:%S"
        )
        zone = match.group("zone")
        if zone == "Z":
            offset = timedelta(0)
        else:
            offset_hours = int(zone[1:3])
            offset_minutes = int(zone[4:6])
            if offset_hours > 23 or offset_minutes > 59:
                raise ValueError
            direction = 1 if zone[0] == "+" else -1
            offset = direction * timedelta(
                hours=offset_hours, minutes=offset_minutes
            )
        utc_time = local_time.replace(tzinfo=timezone(offset)).astimezone(
            timezone.utc
        ).replace(tzinfo=None)
    except (OverflowError, ValueError):
        fail("BUILD_TIME", f"Successful build {field} is not a real timestamp")
    epoch = datetime(1970, 1, 1)
    delta = utc_time - epoch
    epoch_seconds = delta.days * 86400 + delta.seconds
    fraction = match.group("fraction") or ""
    nanoseconds = int(fraction.ljust(9, "0")) if fraction else 0
    return ExactTimestamp(epoch_seconds, nanoseconds)


def _strict_load_path(path: str) -> Any:
    try:
        return strict_loads(Path(path).read_text(encoding="utf-8"))
    except ValidationError:
        raise
    except (OSError, UnicodeError):
        fail("EVIDENCE_FILE", "Evidence file could not be read safely")


def validate_evidence_root(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value.startswith("/")
        or value == "/"
        or not re.fullmatch(r"/[A-Za-z0-9._/-]+", value)
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/")[1:])
    ):
        fail("EVIDENCE_ROOT", "Evidence directory path is unsafe")
    path = Path(value)
    try:
        if path.is_symlink() or not path.is_dir() or os.path.realpath(value) != value:
            fail("EVIDENCE_ROOT", "Evidence directory is not one exact real directory")
    except OSError:
        fail("EVIDENCE_ROOT", "Evidence directory could not be validated")
    return value


def validate_evidence_file_path(root: str, value: Any, *, must_exist: bool) -> str:
    root = validate_evidence_root(root)
    if not isinstance(value, str) or value != value.strip():
        fail("EVIDENCE_FILE", "Evidence file path is unsafe")
    path = Path(value)
    if (
        not path.is_absolute()
        or not re.fullmatch(r"/[A-Za-z0-9._/-]+", value)
        or "//" in value
        or any(part in {"", ".", ".."} for part in path.parts[1:])
        or path.parent.as_posix() != root
        or path.is_symlink()
    ):
        fail("EVIDENCE_FILE", "Evidence file is outside the approved directory")
    if must_exist:
        if not path.is_file():
            fail("EVIDENCE_FILE", "Required evidence file is missing")
    elif path.exists() or path.is_symlink():
        fail("EVIDENCE_FILE_EXISTS", "Evidence output path already exists")
    return value


def validate_revision_document(
    document: Any,
    expected_revision: str,
    expected_digest: str,
    *,
    expected_image: str,
    scope: Scope,
) -> dict[str, str]:
    """Validate exact revision, digest-qualified image, and Ready=True condition."""

    expected_revision = _require_revision_name(expected_revision, "EXPECTED_REVISION")
    expected_digest = _canonical_digest(expected_digest, "EXPECTED_DIGEST")
    expected_image = _canonical_image_uri(expected_image, scope, "EXPECTED_IMAGE")
    root = _require_object(
        document, "REVISION_DOCUMENT", "Revision evidence must be a JSON object"
    )
    _require_exact_keys(root, required={"metadata", "status"})
    metadata = _require_object(
        root["metadata"], "REVISION_METADATA", "Revision metadata is missing or null"
    )
    _require_exact_keys(metadata, required={"name"})
    actual_revision = _require_revision_name(metadata["name"], "REVISION_IDENTITY")
    if actual_revision != expected_revision:
        fail("REVISION_IDENTITY_MISMATCH", "Revision identity differs from expectation")

    status = _require_object(
        root["status"], "REVISION_STATUS", "Revision status is missing or null"
    )
    _require_exact_keys(status, required={"conditions", "imageDigest"})
    observed_image = status["imageDigest"]
    if not isinstance(observed_image, str) or observed_image.count("@") != 1:
        fail("REVISION_IMAGE", "Revision image evidence is not one digest reference")
    actual_image, raw_digest = observed_image.split("@", 1)
    actual_image = _canonical_image_uri(
        actual_image, scope, "REVISION_IMAGE_IDENTITY"
    )
    if actual_image != expected_image:
        fail(
            "REVISION_IMAGE_IDENTITY_MISMATCH",
            "Revision image identity differs from expectation",
        )
    actual_digest = _canonical_digest(raw_digest, "REVISION_DIGEST")
    if observed_image != f"{expected_image}@{actual_digest}":
        fail("REVISION_IMAGE", "Revision image evidence is not canonical")
    if actual_digest != expected_digest:
        fail("REVISION_DIGEST_MISMATCH", "Revision digest differs from expectation")

    conditions = status["conditions"]
    if not isinstance(conditions, list) or not conditions:
        fail("READINESS_MISSING", "Revision conditions are missing or malformed")
    seen_types: set[str] = set()
    ready_statuses: list[str] = []
    allowed_condition_fields = {
        "type",
        "status",
        "reason",
        "message",
        "lastTransitionTime",
        "severity",
    }
    for condition in conditions:
        item = _require_object(
            condition, "CONDITION_MALFORMED", "A revision condition is malformed"
        )
        _require_exact_keys(
            item,
            required={"type", "status"},
            optional=allowed_condition_fields - {"type", "status"},
            code="CONDITION_MALFORMED",
        )
        condition_type = item["type"]
        condition_status = item["status"]
        if not isinstance(condition_type, str) or not condition_type:
            fail("CONDITION_MALFORMED", "A revision condition type is malformed")
        if not isinstance(condition_status, str) or not condition_status:
            fail("CONDITION_MALFORMED", "A revision condition status is malformed")
        for optional_field in allowed_condition_fields - {"type", "status"}:
            if optional_field in item and not isinstance(item[optional_field], str):
                fail("CONDITION_MALFORMED", "A revision condition field is malformed")
        if condition_type in seen_types:
            fail("CONDITION_DUPLICATE", "Revision conditions contain a duplicate type")
        seen_types.add(condition_type)
        if condition_type == "Ready":
            ready_statuses.append(condition_status)

    if len(ready_statuses) != 1:
        fail("READINESS_NOT_UNIQUE", "Revision does not have exactly one Ready condition")
    if ready_statuses[0] != "True":
        fail("READINESS_NOT_TRUE", "Revision Ready condition is not True")
    return {
        "digest": actual_digest,
        "readiness": "READY_TRUE",
        "revision": actual_revision,
    }


@dataclass(frozen=True)
class TrafficTarget:
    target_type: str
    revision: str | None
    percent: int
    tag: str | None

    def raw_record(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "revision": self.revision,
            "tag": self.tag,
            "type": self.target_type,
        }

    def fixed_record(self, latest_ready_revision: str | None) -> dict[str, Any]:
        if self.target_type == "LATEST":
            if latest_ready_revision is None:
                fail("LATEST_UNRESOLVED", "Floating LATEST has no exact resolution")
            revision = latest_ready_revision
        else:
            revision = self.revision
        return {"percent": self.percent, "revision": revision, "tag": self.tag}


@dataclass(frozen=True)
class TrafficState:
    latest_ready_revision: str
    targets: tuple[TrafficTarget, ...]
    latest_created_revision: str | None = None

    def raw_canonical(self) -> str:
        records = sorted(
            (target.raw_record() for target in self.targets),
            key=lambda item: (
                item["type"],
                item["revision"] or "",
                item["tag"] or "",
                item["percent"],
            ),
        )
        return json.dumps(records, sort_keys=True, separators=(",", ":"))

    def effective_records(self, exact_latest_ready_revision: str | None) -> list[dict[str, Any]]:
        if any(target.target_type == "LATEST" for target in self.targets):
            exact = _require_revision_name(
                exact_latest_ready_revision, "LATEST_RESOLUTION"
            )
            if exact != self.latest_ready_revision:
                fail(
                    "LATEST_RESOLUTION_MISMATCH",
                    "Supplied LATEST resolution differs from service evidence",
                )
        else:
            exact = exact_latest_ready_revision
            if exact is not None:
                exact = _require_revision_name(exact, "LATEST_RESOLUTION")

        records = [
            target.fixed_record(exact)
            for target in self.targets
            if target.percent != 0
        ]
        return sorted(
            records,
            key=lambda item: (item["revision"], item["tag"] or "", item["percent"]),
        )

    def effective_canonical(self, exact_latest_ready_revision: str | None) -> str:
        return json.dumps(
            self.effective_records(exact_latest_ready_revision),
            sort_keys=True,
            separators=(",", ":"),
        )


def parse_traffic_document(document: Any) -> TrafficState:
    """Parse a strict service traffic document without emitting service URLs."""

    root = _require_object(
        document, "TRAFFIC_DOCUMENT", "Traffic evidence must be a JSON object"
    )
    _require_exact_keys(root, required={"status"})
    status = _require_object(
        root["status"], "TRAFFIC_STATUS", "Traffic status is missing or null"
    )
    _require_exact_keys(
        status,
        required={"latestReadyRevisionName", "traffic"},
        optional={"latestCreatedRevisionName"},
    )
    latest_ready = _require_revision_name(
        status["latestReadyRevisionName"], "LATEST_READY_REVISION"
    )
    latest_created = None
    if "latestCreatedRevisionName" in status:
        latest_created = _require_revision_name(
            status["latestCreatedRevisionName"], "LATEST_CREATED_REVISION"
        )
    traffic = status["traffic"]
    if not isinstance(traffic, list) or not traffic:
        fail("TRAFFIC_MISSING", "Traffic targets are missing or malformed")

    targets: list[TrafficTarget] = []
    seen_revisions: set[str] = set()
    seen_tags: set[str] = set()
    seen_latest = False
    total = 0
    allowed = {"revisionName", "latestRevision", "percent", "tag", "url"}
    for raw_target in traffic:
        target = _require_object(
            raw_target, "TRAFFIC_TARGET", "A traffic target is malformed"
        )
        if not set(target).issubset(allowed) or "percent" not in target:
            fail("TRAFFIC_TARGET", "A traffic target has missing or unexpected fields")
        if "url" in target and not isinstance(target["url"], str):
            fail("TRAFFIC_TARGET", "A traffic target URL field is malformed")
        percent = target["percent"]
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            fail("TRAFFIC_PERCENT", "A traffic percentage is invalid")
        tag = target.get("tag")
        if tag is not None and (
            not isinstance(tag, str) or not IMAGE_TAG_COMPONENT_RE.fullmatch(tag)
        ):
            fail("TRAFFIC_TAG", "A traffic tag is malformed")
        latest_present = "latestRevision" in target
        latest_value = target.get("latestRevision")
        if latest_present and type(latest_value) is not bool:
            fail("TRAFFIC_TARGET_TYPE", "latestRevision must be an exact Boolean")
        has_latest = latest_value is True
        has_revision = "revisionName" in target
        if has_latest:
            if has_revision:
                resolved_revision = _require_revision_name(
                    target["revisionName"], "TRAFFIC_LATEST_RESOLUTION"
                )
                if resolved_revision != latest_ready:
                    fail(
                        "TRAFFIC_LATEST_RESOLUTION_MISMATCH",
                        "Resolved floating LATEST differs from latest-ready revision",
                    )
            if seen_latest:
                fail("TRAFFIC_DUPLICATE", "Traffic contains repeated LATEST targets")
            seen_latest = True
            target_type = "LATEST"
            revision = None
        else:
            if not has_revision:
                fail(
                    "TRAFFIC_TARGET_TYPE",
                    "A traffic target must identify a fixed revision or floating LATEST",
                )
            target_type = "FIXED"
            revision = _require_revision_name(
                target.get("revisionName"), "TRAFFIC_REVISION"
            )
            if revision in seen_revisions:
                fail("TRAFFIC_DUPLICATE", "A revision appears more than once")
            seen_revisions.add(revision)
        if tag is not None:
            if tag in seen_tags:
                fail("TRAFFIC_DUPLICATE_TAG", "A traffic tag belongs to multiple targets")
            seen_tags.add(tag)
        total += percent
        targets.append(TrafficTarget(target_type, revision, percent, tag))

    if total != 100:
        fail("TRAFFIC_TOTAL", "Traffic percentages do not total exactly 100")
    return TrafficState(latest_ready, tuple(targets), latest_created)


def validate_zero_traffic_transition(
    pre_document: Any,
    post_document: Any,
    *,
    candidate_revision: str,
    baseline_revision: str,
    pre_latest_ready_revision: str,
    pre_approved_revision_evidence: Any | None = None,
    pre_approved_revision_digest: str | None = None,
    pre_approved_revision_image: str | None = None,
    scope: Scope | None = None,
) -> dict[str, Any]:
    """Prove a ready candidate has no production traffic after --no-traffic."""

    candidate = _require_revision_name(candidate_revision, "CANDIDATE_REVISION")
    baseline = _require_revision_name(baseline_revision, "BASELINE_REVISION")
    if candidate == baseline:
        fail("REVISION_COLLISION", "Candidate and baseline revisions are identical")
    pre = parse_traffic_document(pre_document)
    post = parse_traffic_document(post_document)
    if post.latest_created_revision != candidate:
        fail(
            "POST_LATEST_CREATED_MISMATCH",
            "Post-deployment latest-created revision differs from the candidate",
        )
    approved_pre_latest_ready = _require_revision_name(
        pre_latest_ready_revision, "PRE_LATEST_READY_REVISION"
    )
    if pre.latest_ready_revision != approved_pre_latest_ready:
        fail(
            "PRE_LATEST_READY_MISMATCH",
            "Pre-deployment latest-ready revision is not the approved ready revision",
        )
    approval_evidence = (
        pre_approved_revision_evidence,
        pre_approved_revision_digest,
        pre_approved_revision_image,
    )
    if approved_pre_latest_ready == baseline:
        if any(item is not None for item in approval_evidence):
            fail(
                "PRE_LATEST_READY_EVIDENCE_UNEXPECTED",
                "Baseline latest-ready revision must not supply candidate evidence",
            )
    else:
        if not all(item is not None for item in approval_evidence) or scope is None:
            fail(
                "PRE_LATEST_READY_EVIDENCE_REQUIRED",
                "Non-baseline latest-ready revision requires exact revision evidence",
            )
        validate_revision_document(
            pre_approved_revision_evidence,
            approved_pre_latest_ready,
            pre_approved_revision_digest,
            expected_image=pre_approved_revision_image,
            scope=scope,
        )
    if any(target.target_type == "LATEST" for target in pre.targets):
        fail(
            "PRE_FLOATING_LATEST",
            "Pre-deployment traffic must not contain a floating LATEST target",
        )
    if any(target.tag is not None for target in pre.targets):
        fail(
            "PRE_TAGGED_TRAFFIC",
            "Pre-deployment traffic must not contain a tag",
        )
    allowed_post_latest_ready = {baseline, candidate, approved_pre_latest_ready}
    if post.latest_ready_revision not in allowed_post_latest_ready:
        fail(
            "POST_LATEST_READY_MISMATCH",
            "Post-deployment latest-ready revision is not the baseline, candidate, or evidence-bound pre-deployment ready revision",
        )
    if any(target.target_type == "LATEST" for target in post.targets):
        fail(
            "POST_FLOATING_LATEST",
            "Post-deployment traffic must not retain a floating LATEST target",
        )
    if any(target.tag is not None for target in post.targets):
        fail(
            "POST_TAGGED_TRAFFIC",
            "Post-deployment traffic must not contain a tag",
        )

    candidate_post_targets = [
        target
        for target in post.targets
        if target.target_type == "FIXED" and target.revision == candidate
    ]
    if len(candidate_post_targets) > 1:
        fail("TRAFFIC_DUPLICATE", "Candidate has duplicate traffic targets")
    if any(
        target.percent != 0 or target.tag is not None
        for target in candidate_post_targets
    ):
        fail("CANDIDATE_HAS_TRAFFIC", "Candidate receives traffic or has a tag")

    pre_effective = pre.effective_canonical(approved_pre_latest_ready)
    post_effective = post.effective_canonical(baseline)
    if pre_effective != post_effective:
        fail("EFFECTIVE_TRAFFIC_DRIFT", "Effective serving allocation changed")

    expected_post = list(pre.targets)
    post_without_candidate_zero: list[TrafficTarget] = []
    candidate_zero_count = 0
    for target in post.targets:
        if target.target_type == "FIXED" and target.revision == candidate:
            candidate_zero_count += 1
            continue
        post_without_candidate_zero.append(target)
    if candidate_zero_count > 1:
        fail("TRAFFIC_DUPLICATE", "Candidate has duplicate zero-traffic targets")
    expected_canonical = TrafficState(
        post.latest_ready_revision, tuple(expected_post)
    ).raw_canonical()
    actual_canonical = TrafficState(
        post.latest_ready_revision, tuple(post_without_candidate_zero)
    ).raw_canonical()
    if expected_canonical != actual_canonical:
        fail(
            "RAW_TRAFFIC_TRANSFORMATION",
            "Raw traffic changed beyond the documented LATEST-to-fixed transformation",
        )

    baseline_percent = sum(
        item["percent"]
        for item in post.effective_records(baseline)
        if item["revision"] == baseline
    )
    if baseline_percent != 100:
        fail("BASELINE_NOT_100", "Approved baseline does not remain at 100 percent")
    return {
        "baselinePercent": baseline_percent,
        "candidateTraffic": "ABSENT" if candidate_zero_count == 0 else "EXPLICIT_ZERO",
        "effectiveAllocationPreserved": True,
        "latestCreatedRevision": post.latest_created_revision,
        "latestReadyRevision": post.latest_ready_revision,
        "postRaw": post.raw_canonical(),
        "preRaw": pre.raw_canonical(),
    }


def parse_session_secret_document(document: Any) -> dict[str, str]:
    """Select exactly SESSION_SECRET from the approved narrow v2 response."""

    root = _require_object(
        document,
        "SESSION_RESPONSE",
        "Secret-reference evidence must be a JSON object",
    )
    _require_exact_keys(root, required={"template"})
    template = _require_object(
        root["template"], "SESSION_RESPONSE", "Template evidence is missing or null"
    )
    _require_exact_keys(template, required={"containers"})
    containers = template["containers"]
    if not isinstance(containers, list) or not containers:
        fail("SESSION_RESPONSE", "Container evidence is missing or malformed")

    matches: list[dict[str, Any]] = []
    for raw_container in containers:
        container = _require_object(
            raw_container, "SESSION_RESPONSE", "Container evidence is malformed"
        )
        _require_exact_keys(container, required={"env"})
        env = container["env"]
        if not isinstance(env, list):
            fail("SESSION_RESPONSE", "Environment evidence is missing or malformed")
        for raw_entry in env:
            entry = _require_object(
                raw_entry, "SESSION_RESPONSE", "Environment evidence is malformed"
            )
            if "value" in entry:
                fail(
                    "PLAINTEXT_VALUE_REJECTED",
                    "A plaintext environment value field was supplied",
                )
            _require_exact_keys(
                entry,
                required={"name"},
                optional={"valueSource"},
                code="SESSION_RESPONSE",
            )
            name = entry["name"]
            if not isinstance(name, str) or not name:
                fail("SESSION_RESPONSE", "An environment-variable name is malformed")
            if name == "SESSION_SECRET":
                matches.append(entry)

    if not matches:
        fail("BLOCKER_SESSION_SECRET_MISSING", "SESSION_SECRET is missing")
    if len(matches) != 1:
        fail("BLOCKER_SESSION_SECRET_DUPLICATE", "SESSION_SECRET is duplicated")
    match = matches[0]
    if "valueSource" not in match:
        fail(
            "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            "SESSION_SECRET is not a complete Secret Manager reference",
        )
    value_source = _require_object(
        match["valueSource"],
        "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
        "SESSION_SECRET is not a complete Secret Manager reference",
    )
    _require_exact_keys(
        value_source,
        required={"secretKeyRef"},
        code="BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
    )
    reference = _require_object(
        value_source["secretKeyRef"],
        "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
        "SESSION_SECRET is not a complete Secret Manager reference",
    )
    _require_exact_keys(
        reference,
        required={"secret", "version"},
        code="BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
    )
    secret = reference["secret"]
    version = reference["version"]
    if not isinstance(secret, str) or not secret or secret != secret.strip():
        fail(
            "BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE",
            "SESSION_SECRET secret name is missing",
        )
    _validate_numeric_version(
        version, code="BLOCKER_SESSION_SECRET_PLAINTEXT_OR_NON_REFERENCE"
    )
    _validate_secret_reference(secret)
    return {
        "classification": "VALID_SECRET_MANAGER_REFERENCE",
        "name": "SESSION_SECRET",
        "secret": secret,
        "version": version,
    }


def _validate_secret_reference(reference: Any) -> str:
    if (
        not isinstance(reference, str)
        or not reference
        or reference != reference.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in reference)
    ):
        fail("SECRET_REFERENCE", "Secret reference is malformed")
    full_match = SECRET_RESOURCE_RE.fullmatch(reference)
    if full_match:
        return reference
    if not SECRET_ID_RE.fullmatch(reference):
        fail("SECRET_REFERENCE", "Secret reference is malformed")
    return reference


def _validate_numeric_version(value: Any, *, code: str = "VERSION_SELECTOR") -> str:
    if not isinstance(value, str) or not NUMERIC_VERSION_RE.fullmatch(value):
        fail(code, "Version selector must be an exact positive numeric version")
    return value


def _secret_identity(
    reference: str, project: str, project_number: str | None = None
) -> tuple[str, str, str, str]:
    reference = _validate_secret_reference(reference)
    if project_number is None:
        if (
            not isinstance(project, str)
            or not PROJECT_RE.fullmatch(project)
            or project.isdigit()
        ):
            fail("PROJECT_ID", "Project ID is malformed")
        aliases = frozenset((project,))
    else:
        aliases = require_project_identity(project, project_number).aliases
    full_match = SECRET_RESOURCE_RE.fullmatch(reference)
    if full_match:
        observed_project = full_match.group("project")
        if observed_project.isdigit() and project_number is None:
            fail(
                "PROJECT_NUMBER_REQUIRED",
                "Numeric secret project requires a verified project number",
            )
        if observed_project not in aliases:
            fail("SECRET_SCOPE_MISMATCH", "Secret reference identifies another project")
        secret_id = full_match.group("secret")
        observed_resource = reference
    else:
        observed_project = project
        secret_id = reference
        observed_resource = f"projects/{project}/secrets/{secret_id}"
    canonical_resource = f"projects/{project}/secrets/{secret_id}"
    return canonical_resource, observed_resource, observed_project, secret_id


def _secret_resource(
    reference: str, project: str, project_number: str | None = None
) -> str:
    return _secret_identity(reference, project, project_number)[0]


def validate_secret_reference_result(
    document: Any, scope: Scope, project_number: str | None = None
) -> dict[str, Any]:
    """Revalidate one saved SESSION_SECRET handoff before any metadata request."""

    root = _require_object(
        document,
        "SECRET_REFERENCE_RESULT",
        "Saved secret-reference result is malformed",
    )
    _require_exact_keys(
        root,
        required={"classification", "name", "scope", "secret", "version"},
        code="SECRET_REFERENCE_RESULT",
    )
    if (
        root["classification"] != "VALID_SECRET_MANAGER_REFERENCE"
        or root["name"] != "SESSION_SECRET"
    ):
        fail(
            "SECRET_REFERENCE_RESULT",
            "Saved secret-reference result has an invalid classification",
        )
    raw_scope = _require_object(
        root["scope"],
        "SECRET_REFERENCE_SCOPE",
        "Saved secret-reference scope is malformed",
    )
    _require_exact_keys(
        raw_scope,
        required={"project", "region", "service"},
        code="SECRET_REFERENCE_SCOPE",
    )
    observed_scope = require_scope(
        raw_scope["project"], raw_scope["region"], raw_scope["service"]
    )
    if observed_scope != scope:
        fail(
            "SECRET_REFERENCE_SCOPE_MISMATCH",
            "Saved secret-reference scope differs from the authorized scope",
        )
    secret_resource, observed_reference, matched_project, _secret_id = _secret_identity(
        root["secret"], scope.project, project_number
    )
    version = _validate_numeric_version(
        root["version"], code="SECRET_REFERENCE_VERSION"
    )
    return {
        "classification": "VALID_SCOPE_BOUND_SECRET_REFERENCE",
        "name": "SESSION_SECRET",
        "scope": scope.output(),
        "secretResource": secret_resource,
        "observedSecretReference": observed_reference,
        "matchedProjectSegment": matched_project,
        "version": version,
    }


def validate_secret_version_document(
    document: Any,
    *,
    expected_secret: str,
    expected_version: str,
    project: str,
    project_number: str | None = None,
) -> dict[str, str]:
    """Classify exact secret/version metadata without accepting a payload."""

    _validate_numeric_version(expected_version)
    expected_resource, _expected_observed, _expected_project, expected_secret_id = (
        _secret_identity(expected_secret, project, project_number)
    )
    root = _require_object(
        document, "SECRET_METADATA", "Secret-version evidence must be an object"
    )
    _require_exact_keys(
        root,
        required={"requestedSecret", "requestedVersion", "secret", "version"},
    )
    if root["requestedSecret"] != expected_secret or root["requestedVersion"] != expected_version:
        fail("SECRET_REQUEST_MISMATCH", "Secret metadata request binding differs")

    secret = _require_object(
        root["secret"], "SECRET_METADATA", "Secret metadata is malformed"
    )
    _require_exact_keys(secret, required={"result"}, optional={"name"})
    version = _require_object(
        root["version"], "SECRET_VERSION_METADATA", "Version metadata is malformed"
    )
    _require_exact_keys(version, required={"result"}, optional={"name", "state"})

    if secret["result"] == "NOT_FOUND":
        if set(secret) != {"result"}:
            fail("SECRET_METADATA", "Missing secret evidence contains unexpected fields")
        if version != {"result": "NOT_FOUND"}:
            fail(
                "SECRET_METADATA_CONTRADICTION",
                "Missing secret evidence has contradictory version metadata",
            )
        return {"classification": "MISSING_SECRET"}
    if secret["result"] != "FOUND":
        fail("SECRET_METADATA", "Secret metadata is malformed or mismatched")
    secret_name = secret.get("name")
    if not isinstance(secret_name, str):
        fail("SECRET_METADATA", "Secret metadata is malformed or mismatched")
    (
        _secret_canonical,
        observed_secret_resource,
        observed_secret_project,
        observed_secret_id,
    ) = _secret_identity(secret_name, project, project_number)
    if observed_secret_id != expected_secret_id:
        fail("SECRET_METADATA", "Secret metadata is malformed or mismatched")

    if version["result"] == "NOT_FOUND":
        if set(version) != {"result"}:
            fail("SECRET_VERSION_METADATA", "Missing version evidence is malformed")
        return {"classification": "MISSING_VERSION"}
    if version["result"] != "FOUND":
        fail("SECRET_VERSION_METADATA", "Version metadata result is malformed")
    version_name = version.get("name")
    match = VERSION_RESOURCE_RE.fullmatch(version_name) if isinstance(version_name, str) else None
    if match is None:
        fail("SECRET_VERSION_METADATA", "Version metadata identity is malformed")
    (
        _version_canonical,
        observed_version_secret_resource,
        observed_version_project,
        observed_version_secret_id,
    ) = _secret_identity(match.group("secret_resource"), project, project_number)
    if observed_version_secret_id != expected_secret_id:
        fail("SECRET_VERSION_METADATA", "Version metadata identity is malformed")
    if expected_version.isdigit() and match.group("version") != expected_version:
        fail("SECRET_VERSION_MISMATCH", "Version metadata differs from the requested version")
    state = version.get("state")
    classifications = {
        "ENABLED": "EXISTING_ENABLED",
        "DISABLED": "DISABLED",
        "DESTROYED": "DESTROYED",
    }
    if state not in classifications:
        fail("SECRET_VERSION_METADATA", "Version state is malformed or unresolved")
    return {
        "classification": classifications[state],
        "secret": expected_resource,
        "version": match.group("version"),
        "observedSecretResource": observed_secret_resource,
        "observedVersionResource": version_name,
        "matchedSecretProjectSegment": observed_secret_project,
        "matchedVersionProjectSegment": observed_version_project,
    }


def _validate_not_found_body(document: Any) -> None:
    body = _require_object(document, "HTTP_BODY", "HTTP response body is malformed")
    _require_exact_keys(body, required={"error"}, code="HTTP_ERROR")
    error = _require_object(body["error"], "HTTP_ERROR", "HTTP error is malformed")
    _require_exact_keys(
        error, required={"code", "status"}, optional={"message"}, code="HTTP_ERROR"
    )
    if error["code"] != 404 or error["status"] != "NOT_FOUND":
        fail("HTTP_ERROR", "Not-found evidence is contradictory")
    if "message" in error and not isinstance(error["message"], str):
        fail("HTTP_ERROR", "HTTP error message is malformed")


def validate_secret_http_evidence(
    secret_body: Any,
    version_body: Any | None,
    *,
    secret_status: int,
    version_status: int | None,
    expected_secret: str,
    expected_version: str,
    project: str,
    project_number: str | None = None,
) -> dict[str, str]:
    _validate_numeric_version(expected_version)
    expected_resource, _observed, _matched, expected_secret_id = _secret_identity(
        expected_secret, project, project_number
    )
    if secret_status == 404:
        _validate_not_found_body(secret_body)
        if version_status is not None or version_body is not None:
            fail("SECRET_METADATA_CONTRADICTION", "Missing secret has version evidence")
        return {"classification": "MISSING_SECRET"}
    if secret_status in {401, 403}:
        fail("PERMISSION_DENIED", "Secret metadata query was not authorized")
    if secret_status != 200:
        fail("UNEXPECTED_HTTP_STATUS", "Secret metadata query returned an unexpected status")
    secret = _require_object(
        secret_body, "SECRET_METADATA", "Secret metadata response is malformed"
    )
    _require_exact_keys(secret, required={"name"}, code="SECRET_METADATA")
    if not isinstance(secret["name"], str):
        fail("SECRET_METADATA", "Secret metadata identity differs")
    _canonical, _observed, _matched, observed_secret_id = _secret_identity(
        secret["name"], project, project_number
    )
    if observed_secret_id != expected_secret_id:
        fail("SECRET_METADATA", "Secret metadata identity differs")
    if version_status == 404:
        _validate_not_found_body(version_body)
        version = {"result": "NOT_FOUND"}
    elif version_status == 200:
        version_document = _require_object(
            version_body,
            "SECRET_VERSION_METADATA",
            "Secret version response is malformed",
        )
        _require_exact_keys(
            version_document,
            required={"name", "state"},
            code="SECRET_VERSION_METADATA",
        )
        version_name = version_document["name"]
        version_match = (
            VERSION_RESOURCE_RE.fullmatch(version_name)
            if isinstance(version_name, str)
            else None
        )
        if version_match is None:
            fail("SECRET_VERSION_METADATA", "Secret version identity differs")
        _canonical, _observed, _matched, version_secret_id = _secret_identity(
            version_match.group("secret_resource"), project, project_number
        )
        if (
            version_secret_id != expected_secret_id
            or version_match.group("version") != expected_version
        ):
            fail("SECRET_VERSION_METADATA", "Secret version identity differs")
        version = {"result": "FOUND", **version_document}
    elif version_status in {401, 403}:
        fail("PERMISSION_DENIED", "Secret version query was not authorized")
    else:
        fail("UNEXPECTED_HTTP_STATUS", "Secret version query returned an unexpected status")
    return validate_secret_version_document(
        {
            "requestedSecret": expected_secret,
            "requestedVersion": expected_version,
            "secret": {"result": "FOUND", "name": secret["name"]},
            "version": version,
        },
        expected_secret=expected_secret,
        expected_version=expected_version,
        project=project,
        project_number=project_number,
    )


def validate_nonexistence_document(
    document: Any, *, expected_kind: str, expected_resource: str, scope: Scope
) -> dict[str, str]:
    """Accept only an exact API NOT_FOUND result for one candidate resource."""

    if expected_kind not in {"CANDIDATE_TAG", "CANDIDATE_REVISION"}:
        fail("EXISTENCE_KIND", "Candidate resource kind is unsupported")
    revision_pattern = re.escape(scope.service_resource) + r"/revisions/" + REVISION_RE.pattern
    tag_pattern = (
        rf"projects/{re.escape(scope.project)}/locations/{re.escape(scope.region)}/"
        r"repositories/[a-z0-9][a-z0-9._-]*/packages/[a-z0-9][a-z0-9._-]*/"
        r"tags/[A-Za-z0-9._-]+"
    )
    expected_pattern = revision_pattern if expected_kind == "CANDIDATE_REVISION" else tag_pattern
    if not isinstance(expected_resource, str) or not re.fullmatch(
        expected_pattern, expected_resource
    ):
        fail("EXPECTED_RESOURCE", "Expected resource identity is malformed")
    root = _require_object(
        document, "EXISTENCE_EVIDENCE", "Candidate existence evidence is malformed"
    )
    _require_exact_keys(root, required={"httpStatus", "body"})
    status = root["httpStatus"]
    if isinstance(status, bool) or not isinstance(status, int):
        fail("HTTP_STATUS", "HTTP status is malformed")
    body = _require_object(root["body"], "HTTP_BODY", "HTTP response body is malformed")
    if status == 200:
        _require_exact_keys(body, required={"name"})
        if body["name"] != expected_resource:
            fail("RESOURCE_IDENTITY_MISMATCH", "Returned resource identity differs")
        fail(f"{expected_kind}_COLLISION", "Candidate resource already exists")
    if status == 404:
        _require_exact_keys(body, required={"error"})
        error = _require_object(body["error"], "HTTP_ERROR", "HTTP error is malformed")
        _require_exact_keys(
            error, required={"code", "status"}, optional={"message"}, code="HTTP_ERROR"
        )
        if error["code"] != 404 or error["status"] != "NOT_FOUND":
            fail("HTTP_ERROR", "Not-found evidence is contradictory")
        if "message" in error and not isinstance(error["message"], str):
            fail("HTTP_ERROR", "HTTP error message is malformed")
        return {"classification": f"{expected_kind}_AVAILABLE"}
    if status in {401, 403}:
        fail("PERMISSION_DENIED", "Candidate existence query was not authorized")
    fail("UNEXPECTED_HTTP_STATUS", "Candidate existence query did not return an exact result")


def validate_build_submission_document(document: Any) -> dict[str, str]:
    root = _require_object(
        document, "BUILD_SUBMISSION", "Build submission evidence is malformed"
    )
    _require_exact_keys(root, required={"id"}, code="BUILD_SUBMISSION")
    build_id = root["id"]
    if not isinstance(build_id, str) or not BUILD_ID_RE.fullmatch(build_id):
        fail("BUILD_ID", "Submitted build identifier is malformed")
    return {"buildId": build_id, "classification": "BUILD_SUBMITTED"}


def validate_build_config_document(
    document: Any,
    *,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_image_tag: str,
    scope: Scope | None = None,
) -> dict[str, str]:
    """Validate the one exact explicit Cloud Build template."""

    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_sha):
        fail("SOURCE_SHA", "Expected source SHA is malformed")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_tree):
        fail("SOURCE_TREE", "Expected source tree is malformed")
    if scope is None:
        fail("SCOPE", "Build configuration validation requires an authorized scope")
    _image_identity(expected_image_tag, scope)
    root = _require_object(
        document, "BUILD_CONFIG", "Explicit build configuration is malformed"
    )
    _require_exact_keys(
        root,
        required={"steps", "images", "options"},
        code="BUILD_CONFIG_STRUCTURE",
    )
    steps = root["steps"]
    if not isinstance(steps, list) or len(steps) != 1:
        fail("BUILD_CONFIG_STEPS", "Build configuration must contain one step")
    step = _require_object(
        steps[0], "BUILD_CONFIG_STEP", "Build step is malformed"
    )
    _require_exact_keys(
        step, required={"name", "args"}, code="BUILD_CONFIG_STEP"
    )
    if step["name"] != EXPLICIT_BUILD_STEP_NAME:
        fail("BUILD_CONFIG_BUILDER", "Build step uses an unexpected builder")
    if step["args"] != EXPLICIT_BUILD_ARGS:
        fail("BUILD_CONFIG_ARGS", "Docker arguments or provenance labels differ")
    if root["images"] != EXPLICIT_BUILD_IMAGES:
        fail("BUILD_CONFIG_IMAGES", "Build image declaration differs")
    if root["options"] != EXPLICIT_BUILD_OPTIONS:
        fail("BUILD_CONFIG_OPTIONS", "Build substitution policy differs")
    return {
        "classification": "EXPLICIT_BUILD_CONFIG_VALID",
        "imageTag": expected_image_tag,
        "sourceSha": expected_source_sha,
        "sourceTree": expected_source_tree,
    }


def load_build_config_evidence(
    evidence_root: str,
    config_file: str,
    expected_sha256: str,
) -> Any:
    """Load one preserved explicit config and bind its exact bytes to approval."""

    path = validate_evidence_file_path(evidence_root, config_file, must_exist=True)
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        fail("BUILD_CONFIG_DIGEST", "Expected build-config digest is malformed")
    try:
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8", "strict")
    except (OSError, UnicodeError):
        fail("BUILD_CONFIG_FILE", "Build-config evidence could not be read safely")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        fail("BUILD_CONFIG_DIGEST", "Build-config bytes differ from authorization")
    return strict_loads(text)


NONTERMINAL_BUILD_STATES = {"PENDING", "QUEUED", "WORKING"}
FAILED_BUILD_STATES = {
    "FAILURE",
    "INTERNAL_ERROR",
    "TIMEOUT",
    "CANCELLED",
    "EXPIRED",
}


def validate_build_document(
    document: Any,
    *,
    expected_build_id: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_image_tag: str,
    expected_project_number: str,
    expected_service_account: str,
    submitted_config: Any,
    scope: Scope | None = None,
) -> dict[str, str]:
    """Validate one Build resource and its completed BuiltImage output."""

    if not BUILD_ID_RE.fullmatch(expected_build_id):
        fail("BUILD_ID", "Expected build identifier is malformed")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_sha):
        fail("SOURCE_SHA", "Expected source SHA is malformed")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_tree):
        fail("SOURCE_TREE", "Expected source tree is malformed")
    if scope is None:
        fail("SCOPE", "Build validation requires an authorized scope")
    project_identity = require_project_identity(scope.project, expected_project_number)
    identity = _image_identity(expected_image_tag, scope)
    validate_build_config_document(
        submitted_config,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_image_tag=expected_image_tag,
        scope=scope,
    )
    root = _require_object(document, "BUILD_EVIDENCE", "Build evidence is malformed")
    _require_exact_keys(
        root,
        required={
            "name",
            "id",
            "projectId",
            "status",
            "serviceAccount",
            "steps",
            "images",
            "options",
            "substitutions",
            "source",
            "sourceProvenance",
        },
        optional={"createTime", "startTime", "finishTime", "results"},
    )
    expected_names = {
        f"projects/{project}/locations/{scope.region}/builds/{expected_build_id}"
        for project in project_identity.aliases
    }
    if root["name"] not in expected_names or root["projectId"] != scope.project:
        fail("BUILD_RESOURCE_MISMATCH", "Build resource identifies another scope")
    if root["id"] != expected_build_id:
        fail("BUILD_ID_MISMATCH", "Build identifier differs from expectation")
    status = root["status"]
    if not isinstance(status, str):
        fail("BUILD_STATUS", "Build status is malformed")
    expected_service_account_resource = (
        f"projects/{scope.project}/serviceAccounts/{expected_service_account}"
    )
    if root["serviceAccount"] != expected_service_account_resource:
        fail("BUILD_SERVICE_ACCOUNT", "Build service account differs from authorization")
    expected_args = [
        "build", "--tag", expected_image_tag,
        "--label", f"org.opencontainers.image.revision={expected_source_sha}",
        "--label", f"com.calmbydesign.source-tree={expected_source_tree}", ".",
    ]
    steps = root["steps"]
    if steps != [{"name": EXPLICIT_BUILD_STEP_NAME, "args": expected_args}]:
        fail("BUILD_RETURNED_STEPS", "Returned Build step differs from authorization")
    if root["images"] != [expected_image_tag]:
        fail("BUILD_RETURNED_IMAGES", "Returned Build image differs from authorization")
    options = _require_object(
        root["options"], "BUILD_RETURNED_OPTIONS", "Returned Build options are malformed"
    )
    _require_exact_keys(
        options, required=set(), optional={"substitutionOption"},
        code="BUILD_RETURNED_OPTIONS",
    )
    if options.get("substitutionOption", "MUST_MATCH") != "MUST_MATCH":
        fail("BUILD_RETURNED_OPTIONS", "Returned Build permits loose substitutions")
    source = _require_object(root["source"], "BUILD_SOURCE", "Build source is malformed")
    provenance = _require_object(
        root["sourceProvenance"], "BUILD_SOURCE", "Build source provenance is malformed"
    )
    _require_exact_keys(source, required={"storageSource"}, code="BUILD_SOURCE")
    _require_exact_keys(
        provenance, required={"resolvedStorageSource"}, code="BUILD_SOURCE"
    )
    storage = _require_object(source["storageSource"], "BUILD_SOURCE", "Build source is malformed")
    resolved = _require_object(
        provenance["resolvedStorageSource"], "BUILD_SOURCE", "Resolved source is malformed"
    )
    for item in (storage, resolved):
        _require_exact_keys(item, required={"bucket", "object", "generation"}, code="BUILD_SOURCE")
        if (
            item["bucket"] != f"{scope.project}_cloudbuild"
            or not isinstance(item["object"], str)
            or not re.fullmatch(r"source/[A-Za-z0-9._-]+\.tgz", item["object"])
            or not isinstance(item["generation"], str)
            or not re.fullmatch(r"[1-9][0-9]*", item["generation"])
        ):
            fail("BUILD_SOURCE", "Build source identity is malformed")
    if storage != resolved:
        fail("BUILD_SOURCE_MISMATCH", "Resolved Build source differs from submission")
    substitutions = _require_object(
        root["substitutions"], "BUILD_SUBSTITUTIONS", "Build source binding is malformed"
    )
    _require_exact_keys(
        substitutions,
        required={"_SOURCE_SHA", "_SOURCE_TREE", "_CANDIDATE_IMAGE"},
        code="BUILD_SUBSTITUTIONS",
    )
    if substitutions != {
        "_SOURCE_SHA": expected_source_sha,
        "_SOURCE_TREE": expected_source_tree,
        "_CANDIDATE_IMAGE": expected_image_tag,
    }:
        fail("BUILD_SOURCE_MISMATCH", "Build source or image binding differs")
    if status in NONTERMINAL_BUILD_STATES:
        if "finishTime" in root:
            fail("BUILD_STATUS", "Nonterminal build contains terminal result evidence")
        if "results" in root:
            nonterminal_results = _require_object(
                root["results"],
                "BUILD_RESULTS",
                "Nonterminal build results are malformed",
            )
            _require_exact_keys(
                nonterminal_results,
                required=set(),
                optional={"images"},
                code="BUILD_RESULTS",
            )
            result_images = nonterminal_results.get("images", [])
            if not isinstance(result_images, list):
                fail("BUILD_RESULTS", "Nonterminal build result images are malformed")
            if result_images:
                fail("BUILD_STATUS", "Nonterminal build contains terminal result evidence")
        raise NonterminalBuild
    if status in FAILED_BUILD_STATES:
        fail("BUILD_FAILED", "Build reached a non-success terminal state")
    if status != "SUCCESS":
        fail("BUILD_STATUS", "Build status is unknown or contradictory")
    timestamps = {
        field: _parse_build_timestamp(root.get(field), field)
        for field in ("createTime", "startTime", "finishTime")
    }
    if not (
        timestamps["createTime"] <= timestamps["startTime"]
        <= timestamps["finishTime"]
    ):
        fail("BUILD_TIME_ORDER", "Successful build timestamps are nonchronological")
    results = _require_object(
        root.get("results"), "BUILD_RESULTS", "Successful build results are missing"
    )
    _require_exact_keys(results, required={"images"}, code="BUILD_RESULTS")
    built_images = results["images"]
    if not isinstance(built_images, list) or len(built_images) != 1:
        fail("BUILD_RESULT_IMAGES", "Build must contain exactly one BuiltImage")
    built_image = _require_object(
        built_images[0], "BUILT_IMAGE", "BuiltImage evidence is malformed"
    )
    _require_exact_keys(
        built_image,
        required={"name", "digest", "artifactRegistryPackage"},
        optional={"pushTiming", "ociMediaType"},
        code="BUILT_IMAGE",
    )
    if built_image["name"] != identity.tagged_uri:
        fail("BUILT_IMAGE_NAME", "BuiltImage name differs from the authorized tag")
    digest = _canonical_digest(built_image["digest"], "BUILT_IMAGE_DIGEST")
    artifact_package = built_image["artifactRegistryPackage"]
    expected_package_versions = {
        identity.package_resource.replace(
            f"projects/{scope.project}/", f"projects/{project}/", 1
        ) + f"/versions/{digest}"
        for project in project_identity.aliases
    }
    if artifact_package not in expected_package_versions:
        fail(
            "BUILT_IMAGE_PACKAGE",
            "BuiltImage Artifact Registry package differs from authorization",
        )
    if "pushTiming" in built_image:
        push_timing = _require_object(
            built_image["pushTiming"],
            "BUILT_IMAGE_PUSH_TIMING",
            "BuiltImage pushTiming is malformed",
        )
        _require_exact_keys(
            push_timing,
            required={"startTime", "endTime"},
            code="BUILT_IMAGE_PUSH_TIMING",
        )
        push_start = _parse_build_timestamp(push_timing["startTime"], "push startTime")
        push_end = _parse_build_timestamp(push_timing["endTime"], "push endTime")
        if not (
            timestamps["startTime"] <= push_start
            <= push_end <= timestamps["finishTime"]
        ):
            fail(
                "BUILT_IMAGE_PUSH_TIMING_ORDER",
                "BuiltImage pushTiming is outside the build execution interval",
            )
    if "ociMediaType" in built_image:
        oci_media_type = built_image["ociMediaType"]
        if not isinstance(oci_media_type, str) or oci_media_type not in {
            "OCI_MEDIA_TYPE_UNSPECIFIED",
            "IMAGE_MANIFEST",
            "IMAGE_INDEX",
        }:
            fail("BUILT_IMAGE_OCI_MEDIA_TYPE", "BuiltImage OCI media type is malformed")
    return {
        "buildId": expected_build_id,
        "buildResource": root["name"],
        "classification": "BUILD_SUCCESS",
        "createTime": root["createTime"],
        "finishTime": root["finishTime"],
        "imageDigest": digest,
        "imageDigestRef": identity.digest_uri(digest),
        "imageTag": expected_image_tag,
        "packageResource": identity.package_resource,
        "packageVersionResource": artifact_package,
        "sourceSha": expected_source_sha,
        "sourceTree": expected_source_tree,
        "startTime": root["startTime"],
    }


def validate_tag_resolution_document(
    document: Any,
    *,
    expected_image_tag: str,
    expected_project_number: str,
    scope: Scope | None = None,
) -> dict[str, str]:
    """Validate one exact Artifact Registry DockerImage resolved by candidate tag."""

    if scope is None:
        fail("SCOPE", "Tag resolution requires an authorized scope")
    project_identity = require_project_identity(scope.project, expected_project_number)
    identity = _image_identity(expected_image_tag, scope)
    root = _require_object(document, "TAG_EVIDENCE", "Tag evidence is malformed")
    _require_exact_keys(root, required={"name", "uri", "tags"})
    uri = root["uri"]
    prefix = identity.image_uri + "@"
    if (
        not isinstance(uri, str)
        or uri.count("@") != 1
        or not uri.startswith(prefix)
    ):
        fail("TAG_URI", "DockerImage URI differs from the authorized image")
    digest = _canonical_digest(uri[len(prefix):], "TAG_DIGEST")
    if uri != identity.digest_uri(digest):
        fail("TAG_URI", "DockerImage URI is not canonical")
    expected_resources = {
        identity.docker_image_resource(digest).replace(
            f"projects/{scope.project}/", f"projects/{project}/", 1
        )
        for project in project_identity.aliases
    }
    if root["name"] not in expected_resources:
        fail("TAG_IDENTITY_MISMATCH", "DockerImage resource differs from authorization")
    tags = root["tags"]
    if not isinstance(tags, list) or not tags or any(
        not isinstance(tag, str) for tag in tags
    ):
        fail("TAG_LIST", "DockerImage tags are malformed")
    if tags != [identity.tag]:
        fail("TAG_LIST", "Exact bare candidate tag is missing or ambiguous")
    return {
        "classification": "TAG_RESOLVED",
        "digest": digest,
        "dockerImageResource": root["name"],
        "imageDigestRef": identity.digest_uri(digest),
        "imageTag": expected_image_tag,
        "packageResource": identity.package_resource,
    }


def validate_artifact_image_request(
    *, expected_image_tag: str, expected_digest: str, scope: Scope
) -> dict[str, str]:
    """Construct one exact, non-paginated Artifact Registry DockerImage GET."""

    identity = _image_identity(expected_image_tag, scope)
    digest = _canonical_digest(expected_digest, "EXPECTED_DIGEST")
    resource = identity.docker_image_resource(digest)
    return {
        "classification": "ARTIFACT_IMAGE_REQUEST_VALID",
        "digest": digest,
        "resource": resource,
        "url": (
            f"{ARTIFACT_REGISTRY_API_ORIGIN}/{resource}"
            "?fields=name%2Curi%2Ctags"
        ),
    }


def validate_deployment_image_authorization(
    build_document: Any,
    tag_document: Any,
    *,
    expected_build_id: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_image_tag: str,
    expected_project_number: str,
    expected_service_account: str,
    submitted_config: Any,
    scope: Scope,
) -> dict[str, str]:
    build = validate_build_document(
        build_document,
        expected_build_id=expected_build_id,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_image_tag=expected_image_tag,
        expected_project_number=expected_project_number,
        expected_service_account=expected_service_account,
        submitted_config=submitted_config,
        scope=scope,
    )
    tag = validate_tag_resolution_document(
        tag_document,
        expected_image_tag=expected_image_tag,
        expected_project_number=expected_project_number,
        scope=scope,
    )
    if build["imageDigest"] != tag["digest"]:
        fail("IMAGE_DIGEST_MISMATCH", "Build and Artifact Registry digests differ")
    return {
        "buildId": build["buildId"],
        "buildResource": build["buildResource"],
        "classification": "DEPLOYMENT_IMAGE_AUTHORIZED",
        "dockerImageResource": tag["dockerImageResource"],
        "imageDigest": build["imageDigest"],
        "imageDigestRef": build["imageDigestRef"],
        "imageTag": expected_image_tag,
        "packageResource": build["packageResource"],
        "sourceSha": expected_source_sha,
        "sourceTree": expected_source_tree,
    }


def _runtime_string(value: Any, code: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        fail(code, "Runtime metadata contains a malformed string")
    return value


def _runtime_int(value: Any, code: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        fail(code, "Runtime metadata contains a malformed integer")
    return value


def _runtime_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        fail(code, "Runtime metadata contains a malformed boolean")
    return value


def _runtime_string_list(value: Any, code: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, list) or (not allow_empty and not value):
        fail(code, "Runtime metadata contains a malformed list")
    rendered: list[str] = []
    for item in value:
        rendered.append(_runtime_string(item, code))
    if len(rendered) != len(set(rendered)):
        fail(code, "Runtime metadata contains a duplicate list item")


def _validate_runtime_secret_selector(value: Any) -> None:
    selector = _require_object(
        value, "RUNTIME_SECRET_REFERENCE", "Runtime secret reference is malformed"
    )
    _require_exact_keys(
        selector,
        required={"secret", "version"},
        code="RUNTIME_SECRET_REFERENCE",
    )
    _validate_secret_reference(selector["secret"])
    version = _runtime_string(selector["version"], "RUNTIME_SECRET_REFERENCE")
    if not SECRET_ID_RE.fullmatch(version):
        fail("RUNTIME_SECRET_REFERENCE", "Runtime secret version is malformed")


def _validate_runtime_environment(value: Any) -> None:
    if not isinstance(value, list):
        fail("RUNTIME_ENV", "Runtime environment metadata is malformed")
    seen: set[str] = set()
    for raw_entry in value:
        entry = _require_object(
            raw_entry, "RUNTIME_ENV", "Runtime environment entry is malformed"
        )
        _require_exact_keys(
            entry,
            required={"name"},
            optional={"valueSource"},
            code="RUNTIME_ENV",
        )
        name = _runtime_string(entry["name"], "RUNTIME_ENV", maximum=32768)
        if "=" in name or name in seen:
            fail("RUNTIME_ENV", "Runtime environment name is malformed or duplicated")
        seen.add(name)
        if "valueSource" in entry:
            source = _require_object(
                entry["valueSource"],
                "RUNTIME_SECRET_REFERENCE",
                "Runtime environment source is malformed",
            )
            _require_exact_keys(
                source,
                required={"secretKeyRef"},
                code="RUNTIME_SECRET_REFERENCE",
            )
            _validate_runtime_secret_selector(source["secretKeyRef"])


def _validate_runtime_resources(value: Any) -> None:
    resources = _require_object(
        value, "RUNTIME_RESOURCES", "Runtime resources are malformed"
    )
    _require_exact_keys(
        resources,
        required=set(),
        optional={"limits", "cpuIdle", "startupCpuBoost"},
        code="RUNTIME_RESOURCES",
    )
    if not resources:
        fail("RUNTIME_RESOURCES", "Runtime resources are empty")
    if "limits" in resources:
        limits = _require_object(
            resources["limits"], "RUNTIME_LIMITS", "Runtime limits are malformed"
        )
        if not limits or not set(limits).issubset({"cpu", "memory", "nvidia.com/gpu"}):
            fail("RUNTIME_LIMITS", "Runtime limits are missing or unexpected")
        for limit in limits.values():
            _runtime_string(limit, "RUNTIME_LIMITS")
    for key in ("cpuIdle", "startupCpuBoost"):
        if key in resources:
            _runtime_bool(resources[key], "RUNTIME_RESOURCES")


def _validate_runtime_probe(value: Any, *, kind: str) -> None:
    probe = _require_object(value, "RUNTIME_PROBE", "Runtime probe is malformed")
    if kind not in {"startupProbe", "livenessProbe", "readinessProbe"}:
        fail("RUNTIME_PROBE", "Runtime probe kind is unsupported")
    actions = {"httpGet", "tcpSocket", "grpc"}
    timers = {
        "initialDelaySeconds",
        "timeoutSeconds",
        "periodSeconds",
        "failureThreshold",
    }
    _require_exact_keys(
        probe, required=set(), optional=actions | timers, code="RUNTIME_PROBE"
    )
    if len(actions & set(probe)) != 1:
        fail("RUNTIME_PROBE", "Runtime probe must contain exactly one action")
    probe_window_maximum = {
        "startupProbe": 240,
        "livenessProbe": 3600,
        "readinessProbe": 2_147_483_647,
    }[kind]
    timer_bounds = {
        "initialDelaySeconds": (0, probe_window_maximum),
        "periodSeconds": (1, probe_window_maximum),
        "timeoutSeconds": (1, 3600),
        "failureThreshold": (1, 2_147_483_647),
    }
    for key, (minimum, maximum) in timer_bounds.items():
        if key in probe:
            _runtime_int(
                probe[key], "RUNTIME_PROBE", minimum=minimum, maximum=maximum
            )
    if (
        "timeoutSeconds" in probe
        and "periodSeconds" in probe
        and probe["timeoutSeconds"] > probe["periodSeconds"]
    ):
        fail("RUNTIME_PROBE", "Runtime probe timeout exceeds its period")
    if "httpGet" in probe:
        action = _require_object(
            probe["httpGet"], "RUNTIME_PROBE", "Runtime HTTP probe is malformed"
        )
        _require_exact_keys(
            action,
            required=set(),
            optional={"path", "port", "httpHeaders"},
            code="RUNTIME_PROBE",
        )
        if "path" in action:
            _runtime_string(action["path"], "RUNTIME_PROBE")
        if "port" in action:
            _runtime_int(action["port"], "RUNTIME_PROBE", minimum=1, maximum=65535)
        if "httpHeaders" in action:
            headers = action["httpHeaders"]
            if not isinstance(headers, list):
                fail("RUNTIME_PROBE", "Runtime probe headers are malformed")
            for raw_header in headers:
                header = _require_object(
                    raw_header, "RUNTIME_PROBE", "Runtime probe header is malformed"
                )
                _require_exact_keys(
                    header, required={"name"}, code="RUNTIME_PROBE"
                )
                _runtime_string(header["name"], "RUNTIME_PROBE")
    if "tcpSocket" in probe:
        action = _require_object(
            probe["tcpSocket"], "RUNTIME_PROBE", "Runtime TCP probe is malformed"
        )
        _require_exact_keys(
            action, required=set(), optional={"port"}, code="RUNTIME_PROBE"
        )
        if "port" in action:
            _runtime_int(action["port"], "RUNTIME_PROBE", minimum=1, maximum=65535)
    if "grpc" in probe:
        action = _require_object(
            probe["grpc"], "RUNTIME_PROBE", "Runtime gRPC probe is malformed"
        )
        _require_exact_keys(
            action,
            required=set(),
            optional={"port", "service"},
            code="RUNTIME_PROBE",
        )
        if "port" in action:
            _runtime_int(action["port"], "RUNTIME_PROBE", minimum=1, maximum=65535)
        if "service" in action:
            _runtime_string(action["service"], "RUNTIME_PROBE")


def _validate_runtime_volume_mounts(value: Any) -> None:
    if not isinstance(value, list):
        fail("RUNTIME_VOLUME_MOUNT", "Runtime volume mounts are malformed")
    for raw_mount in value:
        mount = _require_object(
            raw_mount, "RUNTIME_VOLUME_MOUNT", "Runtime volume mount is malformed"
        )
        _require_exact_keys(
            mount,
            required={"name", "mountPath"},
            optional={"subPath"},
            code="RUNTIME_VOLUME_MOUNT",
        )
        _runtime_string(mount["name"], "RUNTIME_VOLUME_MOUNT")
        _runtime_string(mount["mountPath"], "RUNTIME_VOLUME_MOUNT")
        if "subPath" in mount:
            _runtime_string(mount["subPath"], "RUNTIME_VOLUME_MOUNT")


def _validate_runtime_container(value: Any, *, require_name: bool) -> None:
    container = _require_object(
        value, "RUNTIME_CONTAINER", "Runtime container is malformed"
    )
    optional = {
        "name",
        "env",
        "resources",
        "ports",
        "startupProbe",
        "livenessProbe",
        "readinessProbe",
        "volumeMounts",
    }
    _require_exact_keys(
        container,
        required={"name"} if require_name else set(),
        optional=optional - ({"name"} if require_name else set()),
        code="RUNTIME_CONTAINER",
    )
    if "name" in container:
        _require_revision_name(container["name"], "RUNTIME_CONTAINER")
    if "env" in container:
        _validate_runtime_environment(container["env"])
    if "resources" in container:
        _validate_runtime_resources(container["resources"])
    if "ports" in container:
        ports = container["ports"]
        if not isinstance(ports, list) or len(ports) != 1:
            fail("RUNTIME_PORT", "Runtime ports are malformed")
        for raw_port in ports:
            port = _require_object(
                raw_port, "RUNTIME_PORT", "Runtime port is malformed"
            )
            _require_exact_keys(
                port,
                required={"containerPort"},
                optional={"name"},
                code="RUNTIME_PORT",
            )
            _runtime_int(
                port["containerPort"], "RUNTIME_PORT", minimum=1, maximum=65535
            )
            if "name" in port and port["name"] not in {"http1", "h2c"}:
                fail("RUNTIME_PORT", "Runtime port protocol is malformed")
    for key in ("startupProbe", "livenessProbe", "readinessProbe"):
        if key in container:
            _validate_runtime_probe(container[key], kind=key)
    if "volumeMounts" in container:
        _validate_runtime_volume_mounts(container["volumeMounts"])


def _validate_runtime_revision_scaling(value: Any) -> None:
    scaling = _require_object(
        value, "RUNTIME_SCALING", "Runtime scaling is malformed"
    )
    allowed = {
        "minInstanceCount",
        "maxInstanceCount",
        "cpuUtilization",
        "concurrencyUtilization",
    }
    _require_exact_keys(
        scaling, required=set(), optional=allowed, code="RUNTIME_SCALING"
    )
    if not scaling:
        fail("RUNTIME_SCALING", "Runtime scaling is empty")
    for key in ("minInstanceCount", "maxInstanceCount"):
        if key in scaling:
            _runtime_int(
                scaling[key], "RUNTIME_SCALING", minimum=0, maximum=2_147_483_647
            )
    if (
        "minInstanceCount" in scaling
        and "maxInstanceCount" in scaling
        and scaling["minInstanceCount"] > scaling["maxInstanceCount"]
    ):
        fail("RUNTIME_SCALING", "Runtime scaling bounds are contradictory")
    utilization_maximum = {
        "cpuUtilization": 0.90,
        "concurrencyUtilization": 0.95,
    }
    for key, maximum in utilization_maximum.items():
        if key in scaling:
            item = scaling[key]
            if (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or (item != 0 and not 0.1 <= item <= maximum)
            ):
                fail("RUNTIME_SCALING", "Runtime utilization is malformed")
    if (
        scaling.get("cpuUtilization") == 0
        and scaling.get("concurrencyUtilization") == 0
        and {"cpuUtilization", "concurrencyUtilization"}.issubset(scaling)
    ):
        fail("RUNTIME_SCALING", "Runtime utilization thresholds are both disabled")


def _validate_runtime_service_scaling(value: Any) -> None:
    scaling = _require_object(
        value, "RUNTIME_SERVICE_SCALING", "Runtime service scaling is malformed"
    )
    _require_exact_keys(
        scaling,
        required=set(),
        optional={
            "manualInstanceCount",
            "maxInstanceCount",
            "minInstanceCount",
            "scalingMode",
        },
        code="RUNTIME_SERVICE_SCALING",
    )
    if not scaling:
        fail("RUNTIME_SERVICE_SCALING", "Runtime service scaling is empty")
    for key in ("manualInstanceCount", "maxInstanceCount", "minInstanceCount"):
        if key in scaling:
            _runtime_int(
                scaling[key],
                "RUNTIME_SERVICE_SCALING",
                minimum=0,
                maximum=2_147_483_647,
            )
    if (
        "minInstanceCount" in scaling
        and "maxInstanceCount" in scaling
        and scaling["minInstanceCount"] > scaling["maxInstanceCount"]
    ):
        fail("RUNTIME_SERVICE_SCALING", "Runtime service scaling bounds contradict")
    if "scalingMode" in scaling and scaling["scalingMode"] not in {
        "SCALING_MODE_UNSPECIFIED",
        "AUTOMATIC",
        "MANUAL",
    }:
        fail("RUNTIME_SERVICE_SCALING", "Runtime service scaling mode is malformed")


def _validate_runtime_vpc(value: Any) -> None:
    vpc = _require_object(value, "RUNTIME_VPC", "Runtime VPC access is malformed")
    _require_exact_keys(
        vpc,
        required=set(),
        optional={"connector", "egress", "networkInterfaces"},
        code="RUNTIME_VPC",
    )
    if not vpc:
        fail("RUNTIME_VPC", "Runtime VPC access is empty")
    if len({"connector", "networkInterfaces"} & set(vpc)) != 1:
        fail("RUNTIME_VPC", "Runtime VPC access must select one network mode")
    if "connector" in vpc:
        _runtime_string(vpc["connector"], "RUNTIME_VPC")
    if "egress" in vpc and vpc["egress"] not in {
        "VPC_EGRESS_UNSPECIFIED",
        "ALL_TRAFFIC",
        "PRIVATE_RANGES_ONLY",
    }:
        fail("RUNTIME_VPC", "Runtime VPC egress is malformed")
    if "networkInterfaces" in vpc:
        interfaces = vpc["networkInterfaces"]
        if not isinstance(interfaces, list) or len(interfaces) != 1:
            fail("RUNTIME_VPC", "Runtime network interfaces are malformed")
        for raw_interface in interfaces:
            interface = _require_object(
                raw_interface, "RUNTIME_VPC", "Runtime network interface is malformed"
            )
            _require_exact_keys(
                interface,
                required=set(),
                optional={"network", "subnetwork", "tags"},
                code="RUNTIME_VPC",
            )
            if not ({"network", "subnetwork"} & set(interface)):
                fail("RUNTIME_VPC", "Runtime network interface has no network identity")
            for key in ("network", "subnetwork"):
                if key in interface:
                    _runtime_string(interface[key], "RUNTIME_VPC")
            if "tags" in interface:
                _runtime_string_list(interface["tags"], "RUNTIME_VPC")


def _validate_runtime_volumes(value: Any) -> None:
    if not isinstance(value, list):
        fail("RUNTIME_VOLUME", "Runtime volumes are malformed")
    sources = {"cloudSqlInstance", "emptyDir", "gcs", "nfs", "secret"}
    for raw_volume in value:
        volume = _require_object(
            raw_volume, "RUNTIME_VOLUME", "Runtime volume is malformed"
        )
        _require_exact_keys(
            volume,
            required={"name"},
            optional=sources,
            code="RUNTIME_VOLUME",
        )
        _runtime_string(volume["name"], "RUNTIME_VOLUME")
        selected = sources & set(volume)
        if len(selected) != 1:
            fail("RUNTIME_VOLUME", "Runtime volume must contain exactly one source")
        source_name = next(iter(selected))
        source = _require_object(
            volume[source_name], "RUNTIME_VOLUME", "Runtime volume source is malformed"
        )
        if source_name == "cloudSqlInstance":
            _require_exact_keys(
                source, required={"instances"}, code="RUNTIME_VOLUME"
            )
            _runtime_string_list(
                source["instances"], "RUNTIME_VOLUME", allow_empty=False
            )
        elif source_name == "emptyDir":
            _require_exact_keys(
                source,
                required=set(),
                optional={"medium", "sizeLimit"},
                code="RUNTIME_VOLUME",
            )
            if "medium" in source and source["medium"] not in {
                "MEDIUM_UNSPECIFIED", "MEMORY", "DISK"
            }:
                fail("RUNTIME_VOLUME", "Runtime emptyDir medium is malformed")
            if "sizeLimit" in source:
                _runtime_string(source["sizeLimit"], "RUNTIME_VOLUME")
        elif source_name == "gcs":
            _require_exact_keys(
                source,
                required={"bucket"},
                optional={"mountOptions", "readOnly"},
                code="RUNTIME_VOLUME",
            )
            _runtime_string(source["bucket"], "RUNTIME_VOLUME")
            if "mountOptions" in source:
                _runtime_string_list(source["mountOptions"], "RUNTIME_VOLUME")
            if "readOnly" in source:
                _runtime_bool(source["readOnly"], "RUNTIME_VOLUME")
        elif source_name == "nfs":
            _require_exact_keys(
                source,
                required={"server", "path"},
                optional={"readOnly"},
                code="RUNTIME_VOLUME",
            )
            _runtime_string(source["server"], "RUNTIME_VOLUME")
            _runtime_string(source["path"], "RUNTIME_VOLUME")
            if "readOnly" in source:
                _runtime_bool(source["readOnly"], "RUNTIME_VOLUME")
        else:
            _require_exact_keys(
                source,
                required={"secret"},
                optional={"defaultMode", "items"},
                code="RUNTIME_VOLUME",
            )
            _validate_secret_reference(source["secret"])
            if "defaultMode" in source:
                _runtime_int(
                    source["defaultMode"], "RUNTIME_VOLUME", minimum=0, maximum=511
                )
            if "items" in source:
                items = source["items"]
                if not isinstance(items, list):
                    fail("RUNTIME_VOLUME", "Runtime secret volume items are malformed")
                for raw_item in items:
                    item = _require_object(
                        raw_item, "RUNTIME_VOLUME", "Runtime secret volume item is malformed"
                    )
                    _require_exact_keys(
                        item,
                        required={"path", "version"},
                        optional={"mode"},
                        code="RUNTIME_VOLUME",
                    )
                    _runtime_string(item["path"], "RUNTIME_VOLUME")
                    version = _runtime_string(item["version"], "RUNTIME_VOLUME")
                    if not SECRET_ID_RE.fullmatch(version):
                        fail("RUNTIME_VOLUME", "Runtime secret volume version is malformed")
                    if "mode" in item:
                        _runtime_int(
                            item["mode"], "RUNTIME_VOLUME", minimum=0, maximum=511
                        )


def validate_runtime_service_document(document: Any, scope: Scope) -> dict[str, Any]:
    root = _require_object(
        document, "RUNTIME_SERVICE", "Runtime service evidence is malformed"
    )
    _require_exact_keys(
        root,
        required={"name", "template"},
        optional={"ingress", "invokerIamDisabled", "iapEnabled", "scaling"},
        code="RUNTIME_SERVICE",
    )
    if root["name"] != scope.service_resource:
        fail("SERVICE_IDENTITY_MISMATCH", "Runtime evidence identifies another service")
    if "ingress" in root and root["ingress"] not in {
        "INGRESS_TRAFFIC_UNSPECIFIED",
        "INGRESS_TRAFFIC_ALL",
        "INGRESS_TRAFFIC_INTERNAL_ONLY",
        "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
        "INGRESS_TRAFFIC_NONE",
    }:
        fail("RUNTIME_SERVICE", "Runtime ingress is malformed")
    for key in ("invokerIamDisabled", "iapEnabled"):
        if key in root:
            _runtime_bool(root[key], "RUNTIME_SERVICE")
    if "scaling" in root:
        _validate_runtime_service_scaling(root["scaling"])
    template = _require_object(
        root["template"], "RUNTIME_TEMPLATE", "Runtime template is malformed"
    )
    _require_exact_keys(
        template,
        required={"containers"},
        optional={
            "serviceAccount",
            "maxInstanceRequestConcurrency",
            "timeout",
            "executionEnvironment",
            "scaling",
            "vpcAccess",
            "volumes",
        },
        code="RUNTIME_TEMPLATE",
    )
    containers = template["containers"]
    if not isinstance(containers, list) or not containers:
        fail("RUNTIME_CONTAINERS", "Runtime containers are missing or malformed")
    multiple_containers = len(containers) > 1
    seen_containers: set[str] = set()
    for container in containers:
        _validate_runtime_container(container, require_name=multiple_containers)
        if multiple_containers:
            name = container["name"]
            if name in seen_containers:
                fail("RUNTIME_CONTAINER", "Runtime container name is duplicated")
            seen_containers.add(name)
    if "serviceAccount" in template:
        _runtime_string(template["serviceAccount"], "RUNTIME_SERVICE_ACCOUNT")
    if "maxInstanceRequestConcurrency" in template:
        _runtime_int(
            template["maxInstanceRequestConcurrency"],
            "RUNTIME_CONCURRENCY",
            minimum=0,
            maximum=1000,
        )
    if "timeout" in template:
        timeout = _runtime_string(template["timeout"], "RUNTIME_TIMEOUT")
        if not DURATION_RE.fullmatch(timeout):
            fail("RUNTIME_TIMEOUT", "Runtime timeout is malformed")
    if "executionEnvironment" in template and template["executionEnvironment"] not in {
        "EXECUTION_ENVIRONMENT_UNSPECIFIED",
        "EXECUTION_ENVIRONMENT_GEN1",
        "EXECUTION_ENVIRONMENT_GEN2",
    }:
        fail("RUNTIME_EXECUTION_ENVIRONMENT", "Runtime execution environment is malformed")
    if "scaling" in template:
        _validate_runtime_revision_scaling(template["scaling"])
    if "vpcAccess" in template:
        _validate_runtime_vpc(template["vpcAccess"])
    if "volumes" in template:
        _validate_runtime_volumes(template["volumes"])
    volumes = template.get("volumes", [])
    volume_names = [volume["name"] for volume in volumes]
    if len(volume_names) != len(set(volume_names)):
        fail("RUNTIME_VOLUME", "Runtime volume name is duplicated")
    for container in containers:
        mounts = container.get("volumeMounts", [])
        mount_names = [mount["name"] for mount in mounts]
        if len(mount_names) != len(set(mount_names)):
            fail("RUNTIME_VOLUME_MOUNT", "Runtime volume mount is duplicated")
        if not set(mount_names).issubset(volume_names):
            fail("RUNTIME_VOLUME_MOUNT", "Runtime volume mount has no matching volume")
    canonical_root = {key: root[key] for key in sorted(root) if key != "name"}
    canonical_template = dict(canonical_root["template"])
    canonical_containers = [dict(container) for container in containers]
    if multiple_containers:
        canonical_containers.sort(key=lambda container: container["name"])
    else:
        canonical_containers[0].pop("name", None)
    canonical_template["containers"] = canonical_containers
    canonical_root["template"] = canonical_template
    canonical = json.dumps(
        canonical_root,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "classification": "RUNTIME_CANONICAL",
        "scope": scope.output(),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def validate_runtime_comparison(
    document: Any, scope: Scope
) -> dict[str, str]:
    root = _require_object(
        document, "RUNTIME_COMPARISON", "Runtime comparison evidence is malformed"
    )
    _require_exact_keys(root, required={"pre", "post"})
    pre = validate_runtime_service_document(root["pre"], scope)
    post = validate_runtime_service_document(root["post"], scope)
    if pre["sha256"] != post["sha256"]:
        fail("RUNTIME_DRIFT", "Runtime configuration changed")
    return {"classification": "RUNTIME_UNCHANGED", "sha256": pre["sha256"]}


def validate_traffic_map_comparison(
    document: Any, *, purpose: str
) -> dict[str, Any]:
    if purpose not in {"TRAFFIC", "ROLLBACK"}:
        fail("MAP_PURPOSE", "Traffic-map comparison purpose is unsupported")
    root = _require_object(
        document, "MAP_COMPARISON", "Traffic-map comparison evidence is malformed"
    )
    _require_exact_keys(root, required={"observed", "expected"})
    observed = parse_traffic_document(root["observed"])
    expected = parse_traffic_document(root["expected"])
    if any(
        target.target_type != "FIXED"
        for state in (observed, expected)
        for target in state.targets
    ):
        fail(
            f"{purpose}_FLOATING_TARGET",
            "Authorized complete map must contain fixed revisions only",
        )
    if observed.latest_ready_revision != expected.latest_ready_revision:
        fail(f"{purpose}_LATEST_MISMATCH", "Latest-ready revision differs from expectation")
    if observed.raw_canonical() != expected.raw_canonical():
        fail(f"{purpose}_MAP_MISMATCH", "Complete traffic map differs from expectation")
    command_map = ",".join(
        f"{target.revision}={target.percent}"
        for target in sorted(expected.targets, key=lambda item: item.revision or "")
    )
    tag_map = ",".join(
        f"{target.tag}={target.revision}"
        for target in sorted(
            (item for item in expected.targets if item.tag is not None),
            key=lambda item: item.tag or "",
        )
    )
    return {
        "classification": f"{purpose}_MAP_MATCH",
        "commandMap": command_map,
        "latestReadyRevision": observed.latest_ready_revision,
        "raw": observed.raw_canonical(),
        "tagMap": tag_map,
    }


def _read_stdin_document() -> Any:
    return strict_loads(sys.stdin.read())


def _emit(result: dict[str, Any]) -> None:
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


def _http_status(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-5][0-9]{2}", value):
        fail("HTTP_STATUS", "HTTP status is malformed")
    return int(value)


def _emit_map_result(result: dict[str, Any], output: str) -> None:
    if output == "command-map":
        print(result["commandMap"])
    elif output == "tag-map":
        print(result["tagMap"] or "-")
    else:
        _emit(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate local, non-secret deployment evidence from stdin"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_scope(target: argparse.ArgumentParser) -> None:
        target.add_argument("--project", required=True)
        target.add_argument("--region", required=True)
        target.add_argument("--service", required=True)

    project_identity_parser = subparsers.add_parser("project-identity")
    add_scope(project_identity_parser)
    project_identity_parser.add_argument("--project-number", required=True)
    project_identity_parser.add_argument(
        "--output", choices=("json", "project-number"), default="json"
    )

    revision_parser = subparsers.add_parser("revision")
    add_scope(revision_parser)
    revision_parser.add_argument("--expected-revision", required=True)
    revision_parser.add_argument("--expected-digest", required=True)
    revision_parser.add_argument("--expected-image", required=True)

    traffic_parser = subparsers.add_parser("traffic")
    add_scope(traffic_parser)
    traffic_parser.add_argument("--latest-ready-revision")

    transition_parser = subparsers.add_parser("zero-traffic")
    add_scope(transition_parser)
    transition_parser.add_argument("--candidate-revision", required=True)
    transition_parser.add_argument("--baseline-revision", required=True)
    transition_parser.add_argument("--pre-latest-ready-revision", required=True)
    transition_parser.add_argument("--evidence-root")
    transition_parser.add_argument("--pre-evidence-file")
    transition_parser.add_argument("--post-evidence-file")
    transition_parser.add_argument("--pre-approved-latest-ready-evidence-file")
    transition_parser.add_argument("--pre-approved-latest-ready-digest")
    transition_parser.add_argument("--pre-approved-latest-ready-image")

    session_parser = subparsers.add_parser("session-secret")
    add_scope(session_parser)
    session_parser.add_argument("--project-number", required=True)
    session_parser.add_argument(
        "--output", choices=("json", "reference"), default="json"
    )

    reference_parser = subparsers.add_parser("secret-reference-result")
    add_scope(reference_parser)
    reference_parser.add_argument("--project-number", required=True)
    reference_parser.add_argument("--evidence-root", required=True)
    reference_parser.add_argument("--input-file", required=True)
    reference_parser.add_argument(
        "--output", choices=("json", "resource-version"), default="json"
    )

    secret_parser = subparsers.add_parser("secret-version")
    add_scope(secret_parser)
    secret_parser.add_argument("--project-number", required=True)
    secret_parser.add_argument("--expected-secret", required=True)
    secret_parser.add_argument("--expected-version", required=True)
    secret_parser.add_argument("--evidence-root")
    secret_parser.add_argument("--secret-status")
    secret_parser.add_argument("--version-status")
    secret_parser.add_argument("--secret-evidence-file")
    secret_parser.add_argument("--version-evidence-file")

    existence_parser = subparsers.add_parser("nonexistence")
    add_scope(existence_parser)
    existence_parser.add_argument(
        "--kind", required=True, choices=("CANDIDATE_TAG", "CANDIDATE_REVISION")
    )
    existence_parser.add_argument("--expected-resource", required=True)
    existence_parser.add_argument("--http-status")

    build_parser = subparsers.add_parser("build")
    add_scope(build_parser)
    build_parser.add_argument("--expected-build-id", required=True)
    build_parser.add_argument("--expected-source-sha", required=True)
    build_parser.add_argument("--expected-source-tree", required=True)
    build_parser.add_argument("--expected-image-tag", required=True)
    build_parser.add_argument("--project-number", required=True)
    build_parser.add_argument("--expected-service-account", required=True)
    build_parser.add_argument("--evidence-root", required=True)
    build_parser.add_argument("--build-config-file", required=True)
    build_parser.add_argument("--expected-build-config-sha256", required=True)
    build_parser.add_argument("--raw", action="store_true")
    build_parser.add_argument(
        "--output", choices=("json", "digest", "image-ref"), default="json"
    )

    config_parser = subparsers.add_parser("build-config")
    add_scope(config_parser)
    config_parser.add_argument("--expected-source-sha", required=True)
    config_parser.add_argument("--expected-source-tree", required=True)
    config_parser.add_argument("--expected-image-tag", required=True)
    config_parser.add_argument(
        "--output", choices=("json", "sha256"), default="json"
    )

    submission_parser = subparsers.add_parser("build-submission")
    add_scope(submission_parser)
    submission_parser.add_argument(
        "--output", choices=("json", "build-id"), default="json"
    )

    tag_parser = subparsers.add_parser("tag-resolution")
    add_scope(tag_parser)
    tag_parser.add_argument("--project-number", required=True)
    tag_parser.add_argument("--expected-image-tag", required=True)
    tag_parser.add_argument("--raw", action="store_true")

    artifact_parser = subparsers.add_parser("artifact-image-request")
    add_scope(artifact_parser)
    artifact_parser.add_argument("--expected-image-tag", required=True)
    artifact_parser.add_argument("--expected-digest", required=True)
    artifact_parser.add_argument(
        "--output", choices=("json", "resource", "url"), default="json"
    )

    authorization_parser = subparsers.add_parser("authorize-image")
    add_scope(authorization_parser)
    authorization_parser.add_argument("--evidence-root", required=True)
    authorization_parser.add_argument("--build-evidence-file", required=True)
    authorization_parser.add_argument("--tag-evidence-file", required=True)
    authorization_parser.add_argument("--expected-build-id", required=True)
    authorization_parser.add_argument("--expected-source-sha", required=True)
    authorization_parser.add_argument("--expected-source-tree", required=True)
    authorization_parser.add_argument("--expected-image-tag", required=True)
    authorization_parser.add_argument("--project-number", required=True)
    authorization_parser.add_argument("--expected-service-account", required=True)
    authorization_parser.add_argument("--build-config-file", required=True)
    authorization_parser.add_argument("--expected-build-config-sha256", required=True)
    authorization_parser.add_argument(
        "--output", choices=("json", "image-ref"), default="json"
    )

    path_parser = subparsers.add_parser("evidence-path")
    add_scope(path_parser)
    path_parser.add_argument("--evidence-root", required=True)
    path_parser.add_argument("--output-file", action="append", default=[])
    path_parser.add_argument("--input-file", action="append", default=[])
    path_parser.add_argument("--expected-image-tag")

    runtime_parser = subparsers.add_parser("runtime-snapshot")
    add_scope(runtime_parser)

    runtime_compare_parser = subparsers.add_parser("runtime-equal")
    add_scope(runtime_compare_parser)
    runtime_compare_parser.add_argument("--evidence-root")
    runtime_compare_parser.add_argument("--pre-evidence-file")
    runtime_compare_parser.add_argument("--post-evidence-file")

    map_parser = subparsers.add_parser("traffic-map")
    add_scope(map_parser)
    map_parser.add_argument("--purpose", required=True, choices=("TRAFFIC", "ROLLBACK"))
    map_parser.add_argument("--evidence-root")
    map_parser.add_argument("--observed-file")
    map_parser.add_argument("--expected-file")
    map_parser.add_argument(
        "--output", choices=("json", "command-map", "tag-map"), default="json"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        scope = require_scope(args.project, args.region, args.service)
        if args.command == "build-config":
            raw = sys.stdin.buffer.read()
            try:
                text = raw.decode("utf-8", "strict")
            except UnicodeDecodeError:
                fail("MALFORMED_JSON", "JSON evidence is malformed or truncated")
            result = validate_build_config_document(
                strict_loads(text),
                expected_source_sha=args.expected_source_sha,
                expected_source_tree=args.expected_source_tree,
                expected_image_tag=args.expected_image_tag,
                scope=scope,
            )
            result["sha256"] = hashlib.sha256(raw).hexdigest()
            result["scope"] = scope.output()
            if args.output == "sha256":
                print(result["sha256"])
            else:
                _emit(result)
            return 0
        if args.command == "artifact-image-request":
            result = validate_artifact_image_request(
                expected_image_tag=args.expected_image_tag,
                expected_digest=args.expected_digest,
                scope=scope,
            )
            result["scope"] = scope.output()
            if args.output == "resource":
                print(result["resource"])
            elif args.output == "url":
                print(result["url"])
            else:
                _emit(result)
            return 0
        if args.command == "secret-reference-result":
            input_path = validate_evidence_file_path(
                args.evidence_root, args.input_file, must_exist=True
            )
            result = validate_secret_reference_result(
                _strict_load_path(input_path), scope, args.project_number
            )
            if args.output == "resource-version":
                print(result["secretResource"], result["version"])
            else:
                _emit(result)
            return 0
        if args.command == "secret-version":
            _validate_numeric_version(args.expected_version)
            _secret_resource(
                args.expected_secret, args.project, args.project_number
            )
        if args.command == "authorize-image":
            build_path = validate_evidence_file_path(
                args.evidence_root, args.build_evidence_file, must_exist=True
            )
            tag_path = validate_evidence_file_path(
                args.evidence_root, args.tag_evidence_file, must_exist=True
            )
            submitted_config = load_build_config_evidence(
                args.evidence_root,
                args.build_config_file,
                args.expected_build_config_sha256,
            )
            result = validate_deployment_image_authorization(
                _strict_load_path(build_path),
                _strict_load_path(tag_path),
                expected_build_id=args.expected_build_id,
                expected_source_sha=args.expected_source_sha,
                expected_source_tree=args.expected_source_tree,
                expected_image_tag=args.expected_image_tag,
                expected_project_number=args.project_number,
                expected_service_account=args.expected_service_account,
                submitted_config=submitted_config,
                scope=scope,
            )
            result["scope"] = scope.output()
            if args.output == "image-ref":
                print(result["imageDigestRef"])
            else:
                _emit(result)
            return 0
        if args.command == "evidence-path":
            validate_evidence_root(args.evidence_root)
            if not args.output_file and not args.input_file:
                fail("EVIDENCE_FILE", "At least one evidence path is required")
            for path in args.output_file:
                validate_evidence_file_path(
                    args.evidence_root, path, must_exist=False
                )
            for path in args.input_file:
                validate_evidence_file_path(
                    args.evidence_root, path, must_exist=True
                )
            if args.expected_image_tag is not None:
                _image_identity(args.expected_image_tag, scope)
            _emit(
                {
                    "classification": "EVIDENCE_PATHS_VALID",
                    "inputCount": len(args.input_file),
                    "outputCount": len(args.output_file),
                    "scope": scope.output(),
                }
            )
            return 0
        if args.command == "secret-version" and any(
            (
                args.evidence_root,
                args.secret_status,
                args.version_status,
                args.secret_evidence_file,
                args.version_evidence_file,
            )
        ):
            if not all(
                (args.evidence_root, args.secret_status, args.secret_evidence_file)
            ):
                fail("EVIDENCE_FILE", "Secret HTTP evidence is incomplete")
            secret_path = validate_evidence_file_path(
                args.evidence_root, args.secret_evidence_file, must_exist=True
            )
            secret_status = _http_status(args.secret_status)
            if secret_status == 404:
                if args.version_status != "SKIPPED" or args.version_evidence_file:
                    fail("SECRET_METADATA_CONTRADICTION", "Missing secret has version evidence")
                version_status = None
                version_body = None
            else:
                if not args.version_status or not args.version_evidence_file:
                    fail("EVIDENCE_FILE", "Secret version HTTP evidence is incomplete")
                version_path = validate_evidence_file_path(
                    args.evidence_root, args.version_evidence_file, must_exist=True
                )
                version_status = _http_status(args.version_status)
                version_body = _strict_load_path(version_path)
            result = validate_secret_http_evidence(
                _strict_load_path(secret_path),
                version_body,
                secret_status=secret_status,
                version_status=version_status,
                expected_secret=args.expected_secret,
                expected_version=args.expected_version,
                project=args.project,
                project_number=args.project_number,
            )
            result["scope"] = scope.output()
            _emit(result)
            return 0

        document: Any
        if args.command == "zero-traffic" and any(
            (
                args.evidence_root,
                args.pre_evidence_file,
                args.post_evidence_file,
                args.pre_approved_latest_ready_evidence_file,
            )
        ):
            if not all(
                (args.evidence_root, args.pre_evidence_file, args.post_evidence_file)
            ):
                fail("EVIDENCE_FILE", "Zero-traffic evidence files are incomplete")
            pre_path = validate_evidence_file_path(
                args.evidence_root, args.pre_evidence_file, must_exist=True
            )
            post_path = validate_evidence_file_path(
                args.evidence_root, args.post_evidence_file, must_exist=True
            )
            envelope = {
                "pre": _strict_load_path(pre_path),
                "post": _strict_load_path(post_path),
            }
            if args.pre_approved_latest_ready_evidence_file:
                approved_path = validate_evidence_file_path(
                    args.evidence_root,
                    args.pre_approved_latest_ready_evidence_file,
                    must_exist=True,
                )
                envelope["preApprovedRevision"] = _strict_load_path(approved_path)
        elif args.command == "runtime-equal" and any(
            (args.evidence_root, args.pre_evidence_file, args.post_evidence_file)
        ):
            if not all(
                (args.evidence_root, args.pre_evidence_file, args.post_evidence_file)
            ):
                fail("EVIDENCE_FILE", "Runtime evidence files are incomplete")
            pre_path = validate_evidence_file_path(
                args.evidence_root, args.pre_evidence_file, must_exist=True
            )
            post_path = validate_evidence_file_path(
                args.evidence_root, args.post_evidence_file, must_exist=True
            )
            result = validate_runtime_comparison(
                {
                    "pre": _strict_load_path(pre_path),
                    "post": _strict_load_path(post_path),
                },
                scope,
            )
            result["scope"] = scope.output()
            _emit(result)
            return 0
        elif args.command == "traffic-map" and any(
            (args.evidence_root, args.observed_file, args.expected_file)
        ):
            if not all((args.evidence_root, args.observed_file, args.expected_file)):
                fail("EVIDENCE_FILE", "Traffic-map evidence files are incomplete")
            observed_path = validate_evidence_file_path(
                args.evidence_root, args.observed_file, must_exist=True
            )
            expected_path = validate_evidence_file_path(
                args.evidence_root, args.expected_file, must_exist=True
            )
            result = validate_traffic_map_comparison(
                {
                    "observed": _strict_load_path(observed_path),
                    "expected": _strict_load_path(expected_path),
                },
                purpose=args.purpose,
            )
            result["scope"] = scope.output()
            _emit_map_result(result, args.output)
            return 0
        else:
            document = _read_stdin_document()

        if args.command == "project-identity":
            result = validate_project_identity_document(
                document,
                expected_project_id=args.project,
                expected_project_number=args.project_number,
            )
            result["scope"] = scope.output()
            if args.output == "project-number":
                print(result["projectNumber"])
                return 0
        elif args.command == "revision":
            scope, evidence = scoped_payload(
                document,
                "revision",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            result = validate_revision_document(
                evidence,
                args.expected_revision,
                args.expected_digest,
                expected_image=args.expected_image,
                scope=scope,
            )
            result["scope"] = scope.output()
        elif args.command == "traffic":
            scope, evidence = scoped_payload(
                document,
                "serviceState",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            state = parse_traffic_document(evidence)
            result = {
                "effective": state.effective_canonical(args.latest_ready_revision),
                "raw": state.raw_canonical(),
                "scope": scope.output(),
            }
        elif args.command == "zero-traffic":
            if not all(
                (args.evidence_root, args.pre_evidence_file, args.post_evidence_file)
            ):
                scope, envelope = scoped_payload(
                    document,
                    "transition",
                    project=args.project,
                    region=args.region,
                    service=args.service,
                )
                envelope = _require_object(
                    envelope,
                    "TRANSITION_DOCUMENT",
                    "Zero-traffic evidence is malformed",
                )
                _require_exact_keys(
                    envelope,
                    required={"pre", "post"},
                    optional={"preApprovedRevision"},
                )
            result = validate_zero_traffic_transition(
                envelope["pre"],
                envelope["post"],
                candidate_revision=args.candidate_revision,
                baseline_revision=args.baseline_revision,
                pre_latest_ready_revision=args.pre_latest_ready_revision,
                pre_approved_revision_evidence=envelope.get("preApprovedRevision"),
                pre_approved_revision_digest=args.pre_approved_latest_ready_digest,
                pre_approved_revision_image=args.pre_approved_latest_ready_image,
                scope=scope,
            )
            result["scope"] = scope.output()
        elif args.command == "session-secret":
            scope, evidence = scoped_payload(
                document,
                "serviceConfig",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            result = parse_session_secret_document(evidence)
            _secret_resource(
                result["secret"], scope.project, args.project_number
            )
            result["scope"] = scope.output()
            if args.output == "reference":
                print(result["secret"], result["version"])
                return 0
        elif args.command == "secret-version":
            scope, evidence = scoped_payload(
                document,
                "secretMetadata",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            result = validate_secret_version_document(
                evidence,
                expected_secret=args.expected_secret,
                expected_version=args.expected_version,
                project=args.project,
                project_number=args.project_number,
            )
            result["scope"] = scope.output()
        elif args.command == "nonexistence":
            if args.http_status is None:
                scope, evidence = scoped_payload(
                    document,
                    "existence",
                    project=args.project,
                    region=args.region,
                    service=args.service,
                )
            else:
                evidence = {"httpStatus": _http_status(args.http_status), "body": document}
            result = validate_nonexistence_document(
                evidence,
                expected_kind=args.kind,
                expected_resource=args.expected_resource,
                scope=scope,
            )
            result["scope"] = scope.output()
        elif args.command == "build":
            submitted_config = load_build_config_evidence(
                args.evidence_root,
                args.build_config_file,
                args.expected_build_config_sha256,
            )
            if args.raw:
                evidence = document
            else:
                scope, evidence = scoped_payload(
                    document,
                    "build",
                    project=args.project,
                    region=args.region,
                    service=args.service,
                )
            result = validate_build_document(
                evidence,
                expected_build_id=args.expected_build_id,
                expected_source_sha=args.expected_source_sha,
                expected_source_tree=args.expected_source_tree,
                expected_image_tag=args.expected_image_tag,
                expected_project_number=args.project_number,
                expected_service_account=args.expected_service_account,
                submitted_config=submitted_config,
                scope=scope,
            )
            result["scope"] = scope.output()
            if args.output == "digest":
                print(result["imageDigest"])
                return 0
            if args.output == "image-ref":
                print(result["imageDigestRef"])
                return 0
        elif args.command == "build-submission":
            scope, evidence = scoped_payload(
                document,
                "buildSubmission",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            result = validate_build_submission_document(evidence)
            result["scope"] = scope.output()
            if args.output == "build-id":
                print(result["buildId"])
            else:
                _emit(result)
            return 0
        elif args.command == "tag-resolution":
            if args.raw:
                evidence = document
            else:
                scope, evidence = scoped_payload(
                    document,
                    "tag",
                    project=args.project,
                    region=args.region,
                    service=args.service,
                )
            result = validate_tag_resolution_document(
                evidence,
                expected_image_tag=args.expected_image_tag,
                expected_project_number=args.project_number,
                scope=scope,
            )
            result["scope"] = scope.output()
        elif args.command == "runtime-snapshot":
            scope, evidence = scoped_payload(
                document,
                "serviceConfig",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            result = validate_runtime_service_document(evidence, scope)
        elif args.command == "runtime-equal":
            scope, evidence = scoped_payload(
                document,
                "comparison",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            result = validate_runtime_comparison(evidence, scope)
            result["scope"] = scope.output()
        else:
            scope, evidence = scoped_payload(
                document,
                "comparison",
                project=args.project,
                region=args.region,
                service=args.service,
            )
            result = validate_traffic_map_comparison(evidence, purpose=args.purpose)
            result["scope"] = scope.output()
        if args.command == "traffic-map":
            _emit_map_result(result, args.output)
        else:
            _emit(result)
        return 0
    except NonterminalBuild:
        _emit({"classification": "BUILD_NONTERMINAL"})
        return 3
    except ValidationError as exc:
        print(f"validation_error={exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "validation_error=INTERNAL_VALIDATION_ERROR: validation failed safely",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
