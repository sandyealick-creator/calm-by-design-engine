# Controlled deployment provenance and rollback runbook

This is an unexecuted future operator procedure. No build, publication,
deployment, live verification, traffic movement, or rollback described here has
been performed or rehearsed. Every Cloud, integration, and participant-related
step requires its own explicit authorization from the project owner.

The phase blocks below are intentionally independent. Run each authorized phase
in a fresh Bash process. Each block defines its own values and validation logic,
enables `set -euo pipefail`, and runs inside a subshell. Never assume a later
phase inherits functions, options, variables, or discovered state from an
earlier block. Copy non-secret identifiers and digests from the approved release
record into each new phase. An unresolved `REPLACE_...` value stops that phase.

Do not paste several phase blocks into one script. Authorization stop points
between phases require an operator to review the prior evidence first.

## 1. Record the exact clean source

Create a new release record, then replace `SOURCE_SHA` with the approved full
commit SHA. This local phase performs no Cloud operation.

```bash
(
  set -euo pipefail
  fail() { printf 'source preflight failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }

  SOURCE_SHA='REPLACE_WITH_FULL_40_CHARACTER_SOURCE_SHA'
  require_value SOURCE_SHA
  [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'SOURCE_SHA format'

  if ! STATUS_OUTPUT="$(git status --porcelain=v1 --untracked-files=all)"; then
    fail 'git status command'
  fi
  [[ -z "$STATUS_OUTPUT" ]] || fail 'working tree or index is not clean'

  if ! HEAD_SHA="$(git rev-parse HEAD)"; then fail 'HEAD lookup'; fi
  [[ "$HEAD_SHA" == "$SOURCE_SHA" ]] || fail 'HEAD does not equal SOURCE_SHA'
  if ! SOURCE_TREE="$(git rev-parse "${SOURCE_SHA}^{tree}")"; then
    fail 'source tree lookup'
  fi
  [[ "$SOURCE_TREE" =~ ^[0-9a-f]{40}$ ]] || fail 'source tree format'

  git show -s --format='%H %T %s' "$SOURCE_SHA" || fail 'source evidence output'
  printf 'source_sha=%s\nsource_tree=%s\n' "$SOURCE_SHA" "$SOURCE_TREE"
)
```

Record `SOURCE_SHA`, `SOURCE_TREE`, the subject, and the empty status result.
Stop if the phase exits nonzero.

## 2. Prove the standard traffic precondition

The standard rollout is valid only when exactly one named revision receives
100% of traffic and no tag or other allocation changes that distribution.

Repository-local evidence does not establish the exact gcloud field schema,
filtered JSON structure, omission behavior, or representation needed to prove
that state safely. This checkpoint therefore has no executable initial-traffic
query. The block below is an intentional unconditional stop, not an invitation
to enter an approval literal or copy traffic values manually.

```bash
(
  set -euo pipefail
  fail() { printf 'initial traffic validation blocked: %s\n' "$1" >&2; exit 1; }
  fail 'verified gcloud traffic schema and machine-parser wiring are unavailable; a reviewed repository correction is required before build or deployment'
)
```

An authorized tooling review must precede a future repository correction. That
review must establish a project-, region-, and service-bound query returning
only the complete traffic allocation in strict JSON. The future parser must
reject empty, null, scalar, truncated, malformed, duplicate, or unexpected
structures and fields. It must require exactly one target with one explicit
`revisionName`, integer `percent` equal to 100, no tag, and no
`latestRevision` assignment. It must derive `PREVIOUS_REVISION` from that parsed
target, query that exact revision through a separately verified filtered schema,
and require one unambiguous immutable `sha256` image digest. The release record
must bind the exact project, region, service, canonical original allocation,
derived revision, and digest before any later activity.

No `TRAFFIC_PREFLIGHT_STATE`, manually copied revision, approval string, or
other operator-entered value can enable the present procedure. A split, tagged,
latest-revision, incomplete, ambiguous, or non-100% original allocation requires
a separately reviewed preservation-and-restoration plan rather than the standard
procedure.

## 3. Secret-reference and runtime-configuration gate

Before candidate deployment, a separately authorized operator must confirm that
the existing service has the approved runtime configuration, all required
Secret Manager references, and `SESSION_SECRET`. Record reference names or
resource references only, never values or payloads.

Cloud Run field shapes can vary by API surface. This runbook does not invent a
secret-inspection query. Use only a separately approved, verified field-filtered
query that exposes reference metadata without plaintext values. After review,
the release record may contain these non-secret gate literals:

- `SECRET_REFERENCE_GATE=APPROVED_REFERENCE_METADATA_ONLY`
- `RUNTIME_CONFIGURATION_GATE=APPROVED_EXISTING_SERVICE_CONFIGURATION`

Secret setup, safe reference inspection, and configuration-inheritance
verification remain deployment blockers until those gates are approved.

## 4. Build from an isolated commit-derived context

This repository has no tracked submodules, symlinks, `.gitmodules`, or
archive-altering `.gitattributes` at this checkpoint. The validator below reads
the exact approved Git tree with NUL-delimited output and stops if any of those
unsupported inputs, any unsupported object mode, or any unsafe path appears.

After separate build authorization, start this phase in a fresh shell and copy
only approved release-record identifiers. It creates a new temporary directory,
archives the exact Git object, compares the archive file list with the tracked
tree, and submits the isolated context instead of the live worktree.

```bash
(
  set -euo pipefail
  fail() { printf 'build phase failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }
  require_component() {
    local name="$1" value="${!1-}"
    require_value "$name"
    [[ "$value" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || fail "$name format"
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  AR_REPOSITORY='REPLACE_WITH_ARTIFACT_REGISTRY_REPOSITORY'
  IMAGE_NAME='REPLACE_WITH_IMAGE_NAME'
  SOURCE_SHA='REPLACE_WITH_RECORDED_SOURCE_SHA'
  SOURCE_TREE='REPLACE_WITH_RECORDED_SOURCE_TREE'
  TRAFFIC_PREFLIGHT_STATE='REPLACE_WITH_APPROVED_PREFLIGHT_STATE'
  PREVIOUS_REVISION='REPLACE_WITH_RECORDED_PREVIOUS_REVISION'
  PREVIOUS_IMAGE_DIGEST='REPLACE_WITH_RECORDED_PREVIOUS_IMAGE_DIGEST'
  PYTHON_BIN='python3.12'
  CANDIDATE_IMAGE_DIGEST=''

  for name in PROJECT_ID REGION AR_REPOSITORY IMAGE_NAME SOURCE_SHA SOURCE_TREE \
    TRAFFIC_PREFLIGHT_STATE PREVIOUS_REVISION PREVIOUS_IMAGE_DIGEST; do
    require_value "$name"
  done
  for name in PROJECT_ID REGION AR_REPOSITORY IMAGE_NAME; do
    require_component "$name"
  done
  [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'SOURCE_SHA format'
  [[ "$SOURCE_TREE" =~ ^[0-9a-f]{40}$ ]] || fail 'SOURCE_TREE format'
  [[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'PREVIOUS_IMAGE_DIGEST format'
  [[ "$TRAFFIC_PREFLIGHT_STATE" == ONE_REVISION_AT_100_NO_TAGS ]] \
    || fail 'standard traffic precondition'

  if ! STATUS_OUTPUT="$(git status --porcelain=v1 --untracked-files=all)"; then
    fail 'git status command'
  fi
  [[ -z "$STATUS_OUTPUT" ]] || fail 'working tree or index is not clean'
  if ! HEAD_SHA="$(git rev-parse HEAD)"; then fail 'HEAD lookup'; fi
  [[ "$HEAD_SHA" == "$SOURCE_SHA" ]] || fail 'HEAD does not equal SOURCE_SHA'
  if ! CURRENT_TREE="$(git rev-parse "${SOURCE_SHA}^{tree}")"; then
    fail 'tree lookup'
  fi
  [[ "$CURRENT_TREE" == "$SOURCE_TREE" ]] || fail 'recorded tree mismatch'
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail 'python3.12 is unavailable'

  fail 'BLOCKED: machine-validated initial traffic evidence is unavailable; a reviewed repository correction is required before build'

  TMP_BASE="${TMPDIR:-/tmp}"
  [[ "$TMP_BASE" == /* && "$TMP_BASE" != / && -d "$TMP_BASE" && -w "$TMP_BASE" ]] \
    || fail 'temporary base is unsafe'
  BUILD_ROOT=''
  if ! BUILD_ROOT="$(mktemp -d \
    "${TMP_BASE%/}/cbd-build.${SOURCE_SHA}.XXXXXXXX")"; then
    fail 'temporary directory creation'
  fi
  [[ -n "$BUILD_ROOT" && -d "$BUILD_ROOT" ]] || fail 'temporary directory missing'
  case "$BUILD_ROOT" in
    "${TMP_BASE%/}/cbd-build.${SOURCE_SHA}."*) ;;
    *) fail 'temporary directory path is unexpected' ;;
  esac

  ARCHIVE_PATH="${BUILD_ROOT}/source.tar"
  BUILD_CONTEXT="${BUILD_ROOT}/context"
  mkdir -- "$BUILD_CONTEXT" || fail 'context directory creation'

  git archive --format=tar --output="$ARCHIVE_PATH" "$SOURCE_SHA" \
    || fail 'git archive creation'
  if ! ARCHIVED_COMMIT="$(git get-tar-commit-id < "$ARCHIVE_PATH")"; then
    fail 'archive commit identification'
  fi
  [[ "$ARCHIVED_COMMIT" == "$SOURCE_SHA" ]] || fail 'archive commit mismatch'

  if ! "$PYTHON_BIN" - "$SOURCE_SHA" "$ARCHIVE_PATH" "$BUILD_CONTEXT" <<'PY'
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath


def stop(message: str) -> None:
    raise SystemExit(f"tree/archive validation failed: {message}")


source_sha, archive_name, context_name = sys.argv[1:]
if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
    stop("source SHA")

context = Path(context_name)
if not context.is_absolute() or not context.is_dir() or any(context.iterdir()):
    stop("context must be a new empty absolute directory")

try:
    result = subprocess.run(
        ["git", "ls-tree", "-rz", source_sha],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
except subprocess.CalledProcessError:
    stop("exact-tree query")

records = result.stdout.split(b"\0")
if not records or records[-1] != b"":
    stop("tree output is not NUL terminated")
records.pop()
if not records:
    stop("approved tree is empty")

tracked: dict[str, tuple[str, str]] = {}
tracked_bytes: set[bytes] = set()
for record in records:
    try:
        header, raw_path = record.split(b"\t", 1)
        mode_raw, type_raw, oid_raw = header.split(b" ", 2)
    except ValueError:
        stop("malformed tree record")
    if not raw_path or raw_path in tracked_bytes:
        stop("empty or duplicate tree path")
    tracked_bytes.add(raw_path)
    try:
        mode = mode_raw.decode("ascii")
        object_type = type_raw.decode("ascii")
        oid = oid_raw.decode("ascii")
        path = raw_path.decode("utf-8", "strict")
    except UnicodeDecodeError:
        stop("unsupported tree encoding")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        stop("unsupported object type or mode")
    if not re.fullmatch(r"[0-9a-f]{40}", oid):
        stop("malformed blob identity")
    if "\n" in path or "\r" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        stop("unsupported control character in tree path")
    if path.startswith("/") or path.endswith("/") or "//" in path:
        stop("absolute or malformed tree path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        stop("tree traversal component")
    if PurePosixPath(path).as_posix() != path:
        stop("tree path normalization changed")
    if parts[-1] in {".gitmodules", ".gitattributes"}:
        stop("unsupported Git metadata input")
    tracked[path] = (mode, oid)

required = {"Dockerfile", "requirements.txt"}
if not required.issubset(tracked):
    stop("required build input is absent from approved tree")

expected_directories: set[str] = set()
for path in tracked:
    parts = path.split("/")
    for index in range(1, len(parts)):
        expected_directories.add("/".join(parts[:index]))

archive_files: dict[str, tarfile.TarInfo] = {}
archive_directories: set[str] = set()
seen_members: set[str] = set()
try:
    archive = tarfile.open(archive_name, mode="r:")
except (OSError, tarfile.TarError):
    stop("archive open")

with archive:
    for member in archive.getmembers():
        raw_name = member.name
        name = raw_name[:-1] if member.isdir() and raw_name.endswith("/") else raw_name
        if not name or name in seen_members:
            stop("empty or duplicate archive member")
        seen_members.add(name)
        if "\n" in name or "\r" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
            stop("unsupported control character in archive path")
        if name.startswith("/") or name.endswith("/") or "//" in name:
            stop("absolute or malformed archive path")
        parts = name.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            stop("archive traversal component")
        if PurePosixPath(name).as_posix() != name:
            stop("archive path normalization changed")
        if member.isdir():
            if name not in expected_directories:
                stop("unexpected archive directory")
            archive_directories.add(name)
        elif member.isfile():
            if name not in tracked:
                stop("unexpected archive file")
            archive_files[name] = member
        else:
            stop("symlink, hard link, device, FIFO, or unsupported archive member")

    if set(archive_files) != set(tracked):
        stop("archive file manifest differs from approved tree")
    if archive_directories != expected_directories:
        stop("archive directory manifest differs from approved tree")

    for path, (mode, oid) in tracked.items():
        member = archive_files[path]
        expected_executable = mode == "100755"
        archive_executable = bool(member.mode & 0o111)
        if archive_executable != expected_executable or member.mode & 0o7000:
            stop("archive executable mode differs from approved tree")
        extracted = archive.extractfile(member)
        if extracted is None:
            stop("archive file cannot be read")
        archive_content = extracted.read()
        try:
            blob = subprocess.run(
                ["git", "cat-file", "blob", oid],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        except subprocess.CalledProcessError:
            stop("approved blob lookup")
        if archive_content != blob:
            stop("archive content differs from approved blob")

    for directory in sorted(expected_directories, key=lambda value: (value.count("/"), value)):
        (context / directory).mkdir(exist_ok=False)
    for path, (mode, _oid) in tracked.items():
        destination = context.joinpath(*path.split("/"))
        member = archive_files[path]
        source = archive.extractfile(member)
        if source is None:
            stop("validated archive file cannot be reopened")
        with destination.open("xb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        os.chmod(destination, 0o755 if mode == "100755" else 0o644)

print(f"validated_tree_files={len(tracked)}")
PY
  then
    fail 'exact-tree, archive, or extraction validation'
  fi

  [[ -f "${BUILD_CONTEXT}/Dockerfile" ]] || fail 'Dockerfile missing from context'
  [[ -f "${BUILD_CONTEXT}/requirements.txt" ]] \
    || fail 'production lock missing from context'

  if ! SENSITIVE_PATH="$(cd "$BUILD_CONTEXT" && find . \
    \( -name '.env' -o -name '*.env' -o -name 'env.yaml' \
       -o -name '*credentials*.json' -o -name 'service-account*.json' \
       -o -name '*-key.json' -o -name '*.pem' -o -name '*.p12' \
       -o -name '*.log' -o -name '*_export.csv' -o -name '*_export.json' \
       -o -path './logs' -o -path './logs/*' \
       -o -path './participant_data' -o -path './participant_data/*' \
       -o -path './scratch' -o -path './scratch/*' \
       -o -path './tmp' -o -path './tmp/*' \) -print -quit)"; then
    fail 'isolated-context sensitive-path scan'
  fi
  [[ -z "$SENSITIVE_PATH" ]] || fail 'sensitive path exists in approved commit'

  IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}"
  if ! gcloud builds submit "$BUILD_CONTEXT" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --tag="${IMAGE_URI}:${SOURCE_SHA}"; then
    fail 'authorized build did not succeed'
  fi

  CANDIDATE_IMAGE_DIGEST=''
  if ! CANDIDATE_IMAGE_DIGEST="$(gcloud artifacts docker images describe \
    "${IMAGE_URI}:${SOURCE_SHA}" \
    --project="$PROJECT_ID" \
    --format='value(image_summary.digest)')"; then
    fail 'candidate digest lookup'
  fi
  [[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'CANDIDATE_IMAGE_DIGEST format'
  CANDIDATE_IMAGE_REF="${IMAGE_URI}@${CANDIDATE_IMAGE_DIGEST}"

  printf 'build_context=%s\nsource_sha=%s\nsource_tree=%s\ncandidate_image=%s\n' \
    "$BUILD_CONTEXT" "$SOURCE_SHA" "$SOURCE_TREE" "$CANDIDATE_IMAGE_REF"
)
```

Record the build identifier, success status, exact command, isolated context
path, source SHA/tree, and immutable image reference. The temporary directory is
newly created and intentionally has no automatic deletion command; removal must
be a separately reviewed operation against the exact recorded path.

The isolated context contains only files tracked by the approved commit, so
ignored and untracked local files are excluded by construction. The committed
`.gcloudignore` may further filter tracked files during upload. Exact gcloud
upload-manifest behavior remains an authorized tooling-verification item; this
procedure does not claim it was live-verified. No build has been performed.

## 5. Validate the candidate name and deploy by digest with zero traffic

`CANDIDATE_REVISION` is the intended full Cloud Run revision name. The deploy
flag receives only the derived suffix. This fresh phase requires a conservative
locally documented name form: lowercase letters, digits, and hyphens; a
lowercase-letter start; no trailing hyphen; and a full name of at most 63
characters. Authorized tooling must confirm these constraints before execution.
After that review, record
`REVISION_NAMING_GATE=APPROVED_CLOUD_RUN_NAMING_CONSTRAINTS`.

Repository-local evidence does not establish a trustworthy gcloud service-scoped
filter, output schema, or zero-result representation for revision collision
checking. Do not guess one. Before this phase, a separately authorized tooling
verification must establish and record an exact field-filtered query that is
bound to project, region, service, and the full candidate revision. It must
distinguish command failure from a successful zero-match result and reject
partial, malformed, duplicate, unrelated, or ambiguous records. A regional list
or the presence of the previous revision is not evidence of candidate absence.

Only a successful, machine-parsed, service-bound zero-match result may
eventually permit deployment. The verified procedure must distinguish command
failure, one exact match, and successful validated zero matches, and must reject
multiple, duplicate, malformed, unrelated, partial, ambiguous, or unexpected
records. Its evidence must bind the exact project, region, service, and full
`CANDIDATE_REVISION`.

That tooling verification has not occurred. The deployment block therefore
terminates unconditionally immediately before construction of the image URI and
the `gcloud run deploy` command. Approval strings, copied scope values, manually
asserted counts, prose confirmation, or replacement placeholders cannot bypass
the stop. Enabling collision clearance requires a separately reviewed repository
correction; operators must not invent replacement values in this runbook.

```bash
(
  set -euo pipefail
  fail() { printf 'candidate deployment failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  AR_REPOSITORY='REPLACE_WITH_ARTIFACT_REGISTRY_REPOSITORY'
  IMAGE_NAME='REPLACE_WITH_IMAGE_NAME'
  SOURCE_SHA='REPLACE_WITH_RECORDED_SOURCE_SHA'
  CANDIDATE_REVISION='REPLACE_WITH_INTENDED_FULL_REVISION_NAME'
  CANDIDATE_IMAGE_DIGEST='REPLACE_WITH_RECORDED_CANDIDATE_IMAGE_DIGEST'
  PREVIOUS_REVISION='REPLACE_WITH_RECORDED_PREVIOUS_REVISION'
  PREVIOUS_IMAGE_DIGEST='REPLACE_WITH_RECORDED_PREVIOUS_IMAGE_DIGEST'
  TRAFFIC_PREFLIGHT_STATE='REPLACE_WITH_APPROVED_PREFLIGHT_STATE'
  SECRET_REFERENCE_GATE='REPLACE_WITH_APPROVED_SECRET_REFERENCE_GATE'
  RUNTIME_CONFIGURATION_GATE='REPLACE_WITH_APPROVED_RUNTIME_CONFIG_GATE'
  REVISION_NAMING_GATE='REPLACE_WITH_APPROVED_NAMING_CONSTRAINT_GATE'

  for name in PROJECT_ID REGION SERVICE AR_REPOSITORY IMAGE_NAME SOURCE_SHA \
    CANDIDATE_REVISION CANDIDATE_IMAGE_DIGEST PREVIOUS_REVISION \
    PREVIOUS_IMAGE_DIGEST TRAFFIC_PREFLIGHT_STATE SECRET_REFERENCE_GATE \
    RUNTIME_CONFIGURATION_GATE REVISION_NAMING_GATE; do
    require_value "$name"
  done
  [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'SOURCE_SHA format'
  [[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'CANDIDATE_IMAGE_DIGEST format'
  [[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'PREVIOUS_IMAGE_DIGEST format'
  [[ "$TRAFFIC_PREFLIGHT_STATE" == ONE_REVISION_AT_100_NO_TAGS ]] \
    || fail 'standard traffic precondition'
  [[ "$SECRET_REFERENCE_GATE" == APPROVED_REFERENCE_METADATA_ONLY ]] \
    || fail 'secret-reference gate'
  [[ "$RUNTIME_CONFIGURATION_GATE" == APPROVED_EXISTING_SERVICE_CONFIGURATION ]] \
    || fail 'runtime-configuration gate'
  [[ "$REVISION_NAMING_GATE" == APPROVED_CLOUD_RUN_NAMING_CONSTRAINTS ]] \
    || fail 'authorized revision-naming verification gate'

  [[ "$SERVICE" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] || fail 'SERVICE format'
  [[ "$CANDIDATE_REVISION" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] \
    || fail 'CANDIDATE_REVISION format'
  (( ${#CANDIDATE_REVISION} <= 63 )) || fail 'CANDIDATE_REVISION length'
  PREFIX="${SERVICE}-"
  [[ "$CANDIDATE_REVISION" == "$PREFIX"* ]] || fail 'service prefix mismatch'
  CANDIDATE_REVISION_SUFFIX="${CANDIDATE_REVISION#${SERVICE}-}"
  [[ -n "$CANDIDATE_REVISION_SUFFIX" ]] || fail 'revision suffix is empty'
  [[ "$CANDIDATE_REVISION_SUFFIX" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] \
    || fail 'revision suffix format'
  [[ "$CANDIDATE_REVISION_SUFFIX" != -* && "$CANDIDATE_REVISION_SUFFIX" != *- ]] \
    || fail 'revision suffix boundary'
  [[ "${SERVICE}-${CANDIDATE_REVISION_SUFFIX}" == "$CANDIDATE_REVISION" ]] \
    || fail 'derived revision mismatch'

  fail 'BLOCKED: verified service-scoped collision query, parser, and zero-result semantics are unavailable; a reviewed repository correction is required before deployment'

  IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}"
  CANDIDATE_IMAGE_REF="${IMAGE_URI}@${CANDIDATE_IMAGE_DIGEST}"
  if ! gcloud run deploy "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --image="$CANDIDATE_IMAGE_REF" \
    --no-traffic \
    --revision-suffix="$CANDIDATE_REVISION_SUFFIX"; then
    fail 'zero-traffic candidate deployment'
  fi
  printf 'candidate_revision=%s\ncandidate_image=%s\n' \
    "$CANDIDATE_REVISION" "$CANDIDATE_IMAGE_REF"
)
```

This command supplies no plaintext settings and changes no explicit IAM,
ingress, scaling, service-account, CPU, memory, or networking flag. Whether the
existing service configuration is inherited exactly remains an authorized
deployment-verification item. `--no-traffic` is mandatory.

## 6. Inspect the exact candidate, Ready condition, and actual traffic

Run this fresh, separately authorized read-only phase after deployment. It
returns only revision identity, conditions, digest, and service traffic status.

```bash
(
  set -euo pipefail
  fail() { printf 'candidate inspection failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  CANDIDATE_REVISION='REPLACE_WITH_RECORDED_FULL_CANDIDATE_REVISION'
  CANDIDATE_IMAGE_DIGEST='REPLACE_WITH_RECORDED_CANDIDATE_IMAGE_DIGEST'
  PREVIOUS_REVISION='REPLACE_WITH_RECORDED_PREVIOUS_REVISION'
  TRAFFIC_PREFLIGHT_STATE='REPLACE_WITH_APPROVED_PREFLIGHT_STATE'

  for name in PROJECT_ID REGION SERVICE CANDIDATE_REVISION \
    CANDIDATE_IMAGE_DIGEST PREVIOUS_REVISION TRAFFIC_PREFLIGHT_STATE; do
    require_value "$name"
  done
  [[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'CANDIDATE_IMAGE_DIGEST format'
  [[ "$TRAFFIC_PREFLIGHT_STATE" == ONE_REVISION_AT_100_NO_TAGS ]] \
    || fail 'standard traffic precondition'

  CANDIDATE_STATUS=''
  if ! CANDIDATE_STATUS="$(gcloud run revisions describe "$CANDIDATE_REVISION" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='yaml(metadata.name,status.conditions.type,status.conditions.status,status.conditions.reason,status.imageDigest)')"; then
    fail 'filtered candidate query'
  fi
  [[ -n "$CANDIDATE_STATUS" ]] || fail 'candidate status is empty'

  TRAFFIC_STATUS=''
  if ! TRAFFIC_STATUS="$(gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='yaml(status.latestReadyRevisionName,status.traffic)')"; then
    fail 'filtered traffic query'
  fi
  [[ -n "$TRAFFIC_STATUS" ]] || fail 'traffic status is empty'
  printf '%s\n%s\n' "$CANDIDATE_STATUS" "$TRAFFIC_STATUS"
)
```

The operator must verify that the returned `metadata.name` is exactly
`CANDIDATE_REVISION`, `status.imageDigest` ends in the recorded digest, and
exactly one condition has `type: Ready` and `status: 'True'`. An absent Ready
condition, `False`, `Unknown`, empty output, multiple or ambiguous Ready values,
wrong revision name, or digest mismatch stops the procedure.

The traffic output must prove that the candidate is absent from traffic or has
zero percent and that the original `PREVIOUS_REVISION` still receives exactly
100% with no tag or other allocation. Do not infer zero traffic only from the
deploy flag.

Only after that evidence is reviewed may the release record contain:

- `CANDIDATE_READY_GATE=READY_TRUE_EXACT_REVISION_AND_DIGEST`
- `ZERO_TRAFFIC_GATE=CANDIDATE_ZERO_PREVIOUS_100_NO_TAGS`

No smoke test, integration test, or traffic movement is permitted for False,
Unknown, absent, empty, ambiguous, or otherwise unapproved readiness evidence.

## 7. Separately authorized candidate testing

Smoke testing and integration testing are separate authorization gates. Use
test-only contacts and records. Verify startup, application health, request-log
privacy, browser redemption and recovery origin, test-only Airtable fields,
approved Gemini behavior, controlled GHL delivery, monitoring, and rollback
visibility. Participant enrollment remains unauthorized.

After separately reviewing successful evidence, the release record may contain:

- `SMOKE_TEST_GATE=APPROVED_TEST_ONLY_SMOKE_EVIDENCE`
- `INTEGRATION_TEST_GATE=APPROVED_TEST_ONLY_INTEGRATION_EVIDENCE`

## 8. Move traffic gradually with a fresh authorization each time

Run this self-contained phase once per separately authorized percentage change.
Copy all bindings from the reviewed release record. Before every movement, an
authorized tooling check must have confirmed the exact filtered JSON field
schema used below and recorded
`TRAFFIC_QUERY_SCHEMA_GATE=APPROVED_FILTERED_TRAFFIC_AND_REVISION_JSON_SCHEMA`.
That gate establishes output structure only; this phase still performs fresh
queries and validates the actual revision identities, immutable digests, Ready
conditions, complete current allocation, and exact authorized from-map.

Each authorization records both canonical maps in candidate-then-previous order,
for example `candidate=0,previous=100` followed by `candidate=10,previous=90`,
using the full revision names rather than those illustrative words. It also
records the exact two revision names and digests. A later increase requires a
new authorization, a new fresh phase, and the prior target as its newly verified
from-map. A historical zero-traffic gate is not valid after traffic has moved.

```bash
(
  set -euo pipefail
  fail() { printf 'traffic movement failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }
  require_component() {
    local name="$1" value="${!1-}"
    require_value "$name"
    [[ "$value" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || fail "$name format"
  }
  validate_state() {
    local expected_candidate_percent="$1" expected_previous_percent="$2"
    "$PYTHON_BIN" - \
      "$CANDIDATE_REVISION" "$CANDIDATE_IMAGE_DIGEST" \
      "$PREVIOUS_REVISION" "$PREVIOUS_IMAGE_DIGEST" \
      "$expected_candidate_percent" "$expected_previous_percent" \
      "$CANDIDATE_STATUS" "$PREVIOUS_STATUS" "$TRAFFIC_STATUS" <<'PY'
import json
import re
import sys


def stop(message: str) -> None:
    raise SystemExit(f"traffic-state validation failed: {message}")


(
    candidate_revision,
    candidate_digest,
    previous_revision,
    previous_digest,
    candidate_percent_raw,
    previous_percent_raw,
    candidate_raw,
    previous_raw,
    traffic_raw,
) = sys.argv[1:]


def document(raw: str, label: str) -> dict:
    if not raw.strip():
        stop(f"{label} output is empty")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        stop(f"{label} output is malformed")
    if not isinstance(value, dict):
        stop(f"{label} output is ambiguous")
    return value


def normalized_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        stop(f"{label} digest is empty or malformed")
    digest = value.rsplit("@", 1)[-1]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        stop(f"{label} digest is not immutable")
    return digest


def validate_revision(raw: str, expected_name: str, expected_digest: str, label: str) -> None:
    value = document(raw, label)
    metadata = value.get("metadata")
    status = value.get("status")
    if not isinstance(metadata, dict) or metadata.get("name") != expected_name:
        stop(f"{label} revision identity mismatch")
    if not isinstance(status, dict):
        stop(f"{label} status is absent")
    if normalized_digest(status.get("imageDigest"), label) != expected_digest:
        stop(f"{label} digest mismatch")
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        stop(f"{label} conditions are absent or malformed")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("type"), str)
        or not isinstance(item.get("status"), str)
        for item in conditions
    ):
        stop(f"{label} condition record is malformed")
    ready = [item for item in conditions if isinstance(item, dict) and item.get("type") == "Ready"]
    if len(ready) != 1 or ready[0].get("status") != "True":
        stop(f"{label} does not have exactly one Ready=True condition")


validate_revision(candidate_raw, candidate_revision, candidate_digest, "candidate")
validate_revision(previous_raw, previous_revision, previous_digest, "previous")

try:
    expected_candidate = int(candidate_percent_raw)
    expected_previous = int(previous_percent_raw)
except ValueError:
    stop("authorized percentages are malformed")

traffic_document = document(traffic_raw, "traffic")
status = traffic_document.get("status")
traffic = status.get("traffic") if isinstance(status, dict) else None
if not isinstance(traffic, list) or not traffic:
    stop("complete traffic allocation is absent or malformed")

allowed_names = {candidate_revision, previous_revision}
allocation: dict[str, int] = {}
allowed_fields = {"revisionName", "percent", "tag", "latestRevision", "url"}
for target in traffic:
    if not isinstance(target, dict) or not set(target).issubset(allowed_fields):
        stop("traffic target is malformed or has an unverified field")
    name = target.get("revisionName")
    percent = target.get("percent")
    if not isinstance(name, str) or name not in allowed_names:
        stop("unexpected, missing, or third revision in traffic")
    if name in allocation:
        stop("duplicate traffic revision")
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        stop("traffic percentage is malformed")
    tag = target.get("tag")
    if tag is not None and tag != "":
        stop("tagged traffic is not permitted")
    latest = target.get("latestRevision")
    if latest is not None and latest is not False:
        stop("latestRevision allocation is ambiguous")
    allocation[name] = percent

actual_candidate = allocation.get(candidate_revision, 0)
actual_previous = allocation.get(previous_revision, 0)
if actual_candidate + actual_previous != 100:
    stop("traffic allocation does not total 100")
if actual_candidate != expected_candidate or actual_previous != expected_previous:
    stop("actual allocation differs from authorized map")

print(
    f"validated_allocation={candidate_revision}={actual_candidate},"
    f"{previous_revision}={actual_previous}"
)
PY
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  PYTHON_BIN='python3.12'
  CANDIDATE_REVISION='REPLACE_WITH_RECORDED_CANDIDATE_REVISION'
  CANDIDATE_IMAGE_DIGEST='REPLACE_WITH_RECORDED_CANDIDATE_IMAGE_DIGEST'
  PREVIOUS_REVISION='REPLACE_WITH_RECORDED_PREVIOUS_REVISION'
  PREVIOUS_IMAGE_DIGEST='REPLACE_WITH_RECORDED_PREVIOUS_IMAGE_DIGEST'
  TRAFFIC_PREFLIGHT_STATE='REPLACE_WITH_APPROVED_PREFLIGHT_STATE'
  TRAFFIC_QUERY_SCHEMA_GATE='REPLACE_WITH_APPROVED_FILTERED_JSON_SCHEMA_GATE'
  SMOKE_TEST_GATE='REPLACE_WITH_APPROVED_SMOKE_GATE'
  INTEGRATION_TEST_GATE='REPLACE_WITH_APPROVED_INTEGRATION_GATE'
  TRAFFIC_AUTHORIZATION='REPLACE_WITH_BOUND_AUTHORIZATION_FOR_THIS_EXACT_CHANGE'
  AUTHORIZED_PROJECT_ID='REPLACE_WITH_AUTHORIZED_PROJECT_ID'
  AUTHORIZED_REGION='REPLACE_WITH_AUTHORIZED_REGION'
  AUTHORIZED_SERVICE='REPLACE_WITH_AUTHORIZED_SERVICE'
  AUTHORIZED_CANDIDATE_REVISION='REPLACE_WITH_AUTHORIZED_CANDIDATE_REVISION'
  AUTHORIZED_CANDIDATE_DIGEST='REPLACE_WITH_AUTHORIZED_CANDIDATE_DIGEST'
  AUTHORIZED_PREVIOUS_REVISION='REPLACE_WITH_AUTHORIZED_PREVIOUS_REVISION'
  AUTHORIZED_PREVIOUS_DIGEST='REPLACE_WITH_AUTHORIZED_PREVIOUS_DIGEST'
  CURRENT_CANDIDATE_PERCENT='REPLACE_WITH_AUTHORIZED_CURRENT_CANDIDATE_PERCENT'
  CURRENT_PREVIOUS_PERCENT='REPLACE_WITH_AUTHORIZED_CURRENT_PREVIOUS_PERCENT'
  TARGET_CANDIDATE_PERCENT='REPLACE_WITH_AUTHORIZED_TARGET_CANDIDATE_PERCENT'
  TARGET_PREVIOUS_PERCENT='REPLACE_WITH_AUTHORIZED_TARGET_PREVIOUS_PERCENT'
  AUTHORIZED_CURRENT_ALLOCATION='REPLACE_WITH_EXACT_CANONICAL_FROM_MAP'
  AUTHORIZED_TARGET_ALLOCATION='REPLACE_WITH_EXACT_CANONICAL_TARGET_MAP'

  for name in PROJECT_ID REGION SERVICE CANDIDATE_REVISION \
    CANDIDATE_IMAGE_DIGEST PREVIOUS_REVISION PREVIOUS_IMAGE_DIGEST \
    TRAFFIC_PREFLIGHT_STATE TRAFFIC_QUERY_SCHEMA_GATE SMOKE_TEST_GATE \
    INTEGRATION_TEST_GATE TRAFFIC_AUTHORIZATION AUTHORIZED_PROJECT_ID \
    AUTHORIZED_REGION AUTHORIZED_SERVICE AUTHORIZED_CANDIDATE_REVISION \
    AUTHORIZED_CANDIDATE_DIGEST AUTHORIZED_PREVIOUS_REVISION \
    AUTHORIZED_PREVIOUS_DIGEST CURRENT_CANDIDATE_PERCENT \
    CURRENT_PREVIOUS_PERCENT TARGET_CANDIDATE_PERCENT TARGET_PREVIOUS_PERCENT \
    AUTHORIZED_CURRENT_ALLOCATION AUTHORIZED_TARGET_ALLOCATION; do
    require_value "$name"
  done
  for name in PROJECT_ID REGION; do require_component "$name"; done
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail 'python3.12 is unavailable'
  [[ "$SERVICE" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] || fail 'SERVICE format'
  [[ "$CANDIDATE_REVISION" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] \
    || fail 'candidate revision format'
  [[ "$PREVIOUS_REVISION" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] \
    || fail 'previous revision format'
  [[ "$CANDIDATE_REVISION" == "${SERVICE}-"* ]] || fail 'candidate service prefix'
  [[ "$PREVIOUS_REVISION" == "${SERVICE}-"* ]] || fail 'previous service prefix'
  [[ "$CANDIDATE_REVISION" != "$PREVIOUS_REVISION" ]] || fail 'revisions are identical'
  (( ${#CANDIDATE_REVISION} <= 63 && ${#PREVIOUS_REVISION} <= 63 )) \
    || fail 'revision length'
  [[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'candidate digest format'
  [[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'previous digest format'
  [[ "$TRAFFIC_PREFLIGHT_STATE" == ONE_REVISION_AT_100_NO_TAGS ]] \
    || fail 'standard traffic precondition'
  [[ "$TRAFFIC_QUERY_SCHEMA_GATE" == APPROVED_FILTERED_TRAFFIC_AND_REVISION_JSON_SCHEMA ]] \
    || fail 'filtered traffic and revision schema gate'
  [[ "$SMOKE_TEST_GATE" == APPROVED_TEST_ONLY_SMOKE_EVIDENCE ]] \
    || fail 'smoke-test gate'
  [[ "$INTEGRATION_TEST_GATE" == APPROVED_TEST_ONLY_INTEGRATION_EVIDENCE ]] \
    || fail 'integration-test gate'
  [[ "$TRAFFIC_AUTHORIZATION" == APPROVED_EXACT_SCOPE_REVISIONS_DIGESTS_FROM_MAP_AND_TARGET_MAP ]] \
    || fail 'bound traffic authorization'
  [[ "$AUTHORIZED_PROJECT_ID" == "$PROJECT_ID" ]] \
    || fail 'authorized project binding'
  [[ "$AUTHORIZED_REGION" == "$REGION" ]] \
    || fail 'authorized region binding'
  [[ "$AUTHORIZED_SERVICE" == "$SERVICE" ]] \
    || fail 'authorized service binding'
  [[ "$AUTHORIZED_CANDIDATE_REVISION" == "$CANDIDATE_REVISION" ]] \
    || fail 'authorized candidate revision binding'
  [[ "$AUTHORIZED_CANDIDATE_DIGEST" == "$CANDIDATE_IMAGE_DIGEST" ]] \
    || fail 'authorized candidate digest binding'
  [[ "$AUTHORIZED_PREVIOUS_REVISION" == "$PREVIOUS_REVISION" ]] \
    || fail 'authorized previous revision binding'
  [[ "$AUTHORIZED_PREVIOUS_DIGEST" == "$PREVIOUS_IMAGE_DIGEST" ]] \
    || fail 'authorized previous digest binding'
  [[ "$CURRENT_CANDIDATE_PERCENT" =~ ^(0|[1-9]|[1-9][0-9])$ ]] \
    || fail 'current candidate percentage format'
  [[ "$CURRENT_PREVIOUS_PERCENT" =~ ^([1-9]|[1-9][0-9]|100)$ ]] \
    || fail 'current previous percentage format'
  [[ "$TARGET_CANDIDATE_PERCENT" =~ ^([1-9]|[1-9][0-9])$ ]] \
    || fail 'target candidate percentage format'
  [[ "$TARGET_PREVIOUS_PERCENT" =~ ^([1-9]|[1-9][0-9])$ ]] \
    || fail 'target previous percentage format'
  (( CURRENT_CANDIDATE_PERCENT + CURRENT_PREVIOUS_PERCENT == 100 )) \
    || fail 'current percentages do not total 100'
  (( TARGET_CANDIDATE_PERCENT + TARGET_PREVIOUS_PERCENT == 100 )) \
    || fail 'target percentages do not total 100'
  (( TARGET_CANDIDATE_PERCENT > CURRENT_CANDIDATE_PERCENT )) \
    || fail 'target is not a gradual candidate increase'
  EXPECTED_CURRENT_ALLOCATION="${CANDIDATE_REVISION}=${CURRENT_CANDIDATE_PERCENT},${PREVIOUS_REVISION}=${CURRENT_PREVIOUS_PERCENT}"
  EXPECTED_TARGET_ALLOCATION="${CANDIDATE_REVISION}=${TARGET_CANDIDATE_PERCENT},${PREVIOUS_REVISION}=${TARGET_PREVIOUS_PERCENT}"
  [[ "$AUTHORIZED_CURRENT_ALLOCATION" == "$EXPECTED_CURRENT_ALLOCATION" ]] \
    || fail 'authorized current allocation binding'
  [[ "$AUTHORIZED_TARGET_ALLOCATION" == "$EXPECTED_TARGET_ALLOCATION" ]] \
    || fail 'authorized target allocation binding'

  fail 'BLOCKED: machine-validated initial traffic evidence is unavailable; a reviewed repository correction is required before traffic movement'

  CANDIDATE_STATUS=''
  if ! CANDIDATE_STATUS="$(gcloud run revisions describe "$CANDIDATE_REVISION" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='json(metadata.name,status.conditions,status.imageDigest)')"; then
    fail 'fresh filtered candidate query'
  fi
  [[ -n "$CANDIDATE_STATUS" ]] || fail 'candidate query returned empty output'
  PREVIOUS_STATUS=''
  if ! PREVIOUS_STATUS="$(gcloud run revisions describe "$PREVIOUS_REVISION" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='json(metadata.name,status.conditions,status.imageDigest)')"; then
    fail 'fresh filtered previous-revision query'
  fi
  [[ -n "$PREVIOUS_STATUS" ]] || fail 'previous query returned empty output'
  TRAFFIC_STATUS=''
  if ! TRAFFIC_STATUS="$(gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='json(status.traffic)')"; then
    fail 'fresh filtered current-traffic query'
  fi
  [[ -n "$TRAFFIC_STATUS" ]] || fail 'current traffic query returned empty output'
  if ! validate_state "$CURRENT_CANDIDATE_PERCENT" "$CURRENT_PREVIOUS_PERCENT"; then
    fail 'current revision or traffic state differs from authorization'
  fi

  if ! gcloud run services update-traffic "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --to-revisions="$AUTHORIZED_TARGET_ALLOCATION"; then
    fail 'authorized traffic update'
  fi
  TRAFFIC_STATUS=''
  if ! TRAFFIC_STATUS="$(gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='json(status.traffic)')"; then
    fail 'filtered post-movement traffic query'
  fi
  [[ -n "$TRAFFIC_STATUS" ]] || fail 'post-movement traffic output is empty'
  if ! validate_state "$TARGET_CANDIDATE_PERCENT" "$TARGET_PREVIOUS_PERCENT"; then
    fail 'post-movement allocation differs from authorized target'
  fi
)
```

Stop after each movement for the approved observation period. Preserve filtered
evidence without secret or participant content. A post-movement mismatch or a
failed observation stops all further increases and requires the separately
authorized rollback procedure; do not automatically issue another traffic
command. Do not move 100% through this gradual block; final cutover requires its
own reviewed procedure.

## 9. Roll back to the verified pre-release state

This standard rollback is valid only because preflight proved that the original
state was one named revision at 100% with no tags. It restores that exact state,
not an arbitrarily selected older revision. A preexisting split requires its
separate approved full-map rollback plan instead.

Repository-local evidence does not establish the exact gcloud schemas needed to
query and prove rollback state safely. The present rollback procedure therefore
terminates unconditionally and contains no traffic-changing command. Approval
literals, copied revision names, manually described allocations, or sample
values cannot enable rollback. A separately reviewed repository correction is
required after authorized tooling verification.

That future correction must use minimum-field, project-, region-, and
service-bound JSON queries. Before any traffic command, its parser must:

- verify that the recorded original allocation is exactly the one explicit
  `PREVIOUS_REVISION` at integer 100%, with no tag or `latestRevision`;
- query that exact revision, require its immutable digest to equal
  `PREVIOUS_IMAGE_DIGEST`, and require exactly one condition whose `type` is
  `Ready` and whose `status` is exactly `True`;
- reject absent, False, Unknown, duplicate, malformed, partial, or mismatched
  readiness evidence;
- parse the complete current allocation, rejecting empty, null, scalar,
  truncated, malformed, duplicate, incomplete, tagged, latest-revision, third-
  revision, unexpected-field, non-integer, out-of-range, or non-100% states;
- canonicalize the complete observed current map and require rollback
  authorization to bind the exact project, region, service, release-record
  identity, previous revision and digest, observed map, and exact original target
  map; and
- permit `--to-revisions="${PREVIOUS_REVISION}=100"` only for that validated
  standard original allocation. Any other original allocation requires its own
  separately reviewed full-map restoration plan.

After the one separately authorized rollback command, the future parser must
query the complete allocation again and require exact equality with the recorded
original map. Command failure, observation failure, malformed output, drift, or
final mismatch is rollback failure and must not trigger another traffic command.
Evidence must contain only non-secret release identifiers and must not contain
participant data.

```bash
(
  set -euo pipefail
  fail() { printf 'rollback failed: %s\n' "$1" >&2; exit 1; }
  fail 'BLOCKED: verified rollback schemas and complete pre/post-state parser wiring are unavailable; a reviewed repository correction is required before rollback'
)
```

After a future successful rollback, record the verified original map, observed
pre-rollback map, final map, reason, time, operator, impact, and follow-up
decision. Preserve the candidate revision and image for investigation unless a
separate retention or security authorization requires otherwise.

## 10. Remaining gates

Before deployment or participant enrollment, the owner must still authorize and
verify the gcloud command/output schemas, exact upload behavior, Linux/container
build, Artifact Registry permissions, Secret Manager references, required
runtime configuration, candidate smoke and integration behavior, Cloud Run
proxy/log privacy, Airtable/GHL compatibility, delivery, approved Gemini use,
browser and recovery behavior, monitoring, and rollback rehearsal.

Participant enrollment remains unauthorized.
