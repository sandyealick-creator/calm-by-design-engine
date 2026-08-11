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

### 2.1 Capture the complete filtered traffic status

This separately authorized read-only phase requests service status only. It
does not request the service specification or environment configuration.

```bash
(
  set -euo pipefail
  fail() { printf 'traffic inspection failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  require_value PROJECT_ID
  require_value REGION
  require_value SERVICE

  gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='yaml(status.latestReadyRevisionName,status.traffic)' \
    || fail 'filtered service query'
)
```

Record the complete returned traffic status. The operator must confirm all of
the following before setting the release-record gate:

- exactly one traffic entry names a revision;
- that revision receives exactly 100%;
- no tagged allocation is present;
- no tag, latest-revision allocation, or omitted value makes the map ambiguous.

If the map is empty, incomplete, tagged, ambiguous, or split across revisions,
set no gate and stop the standard procedure before building. Split traffic
requires a separately reviewed rollout and rollback plan that records and
restores the complete original allocation.

Only a verified one-revision, 100%, untagged state may be recorded as:

`TRAFFIC_PREFLIGHT_STATE=ONE_REVISION_AT_100_NO_TAGS`

### 2.2 Record that exact revision and immutable image digest

In a fresh read-only phase, copy the exact revision and approved gate from the
release record. A stale or unresolved value must not be reused.

```bash
(
  set -euo pipefail
  fail() { printf 'rollback-target capture failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  TRAFFIC_PREFLIGHT_STATE='REPLACE_WITH_APPROVED_PREFLIGHT_STATE'
  PREVIOUS_REVISION='REPLACE_WITH_THE_EXACT_100_PERCENT_REVISION'
  PREVIOUS_IMAGE_DIGEST=''

  for name in PROJECT_ID REGION SERVICE TRAFFIC_PREFLIGHT_STATE PREVIOUS_REVISION; do
    require_value "$name"
  done
  [[ "$TRAFFIC_PREFLIGHT_STATE" == ONE_REVISION_AT_100_NO_TAGS ]] \
    || fail 'standard rollout traffic precondition was not proved'
  [[ "$PREVIOUS_REVISION" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] \
    || fail 'PREVIOUS_REVISION format'
  (( ${#PREVIOUS_REVISION} <= 63 )) || fail 'PREVIOUS_REVISION length'

  PREVIOUS_IMAGE_ID=''
  if ! PREVIOUS_IMAGE_ID="$(gcloud run revisions describe "$PREVIOUS_REVISION" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(status.imageDigest)')"; then
    fail 'previous revision digest query'
  fi
  [[ -n "$PREVIOUS_IMAGE_ID" ]] || fail 'previous image identity is empty'
  case "$PREVIOUS_IMAGE_ID" in
    sha256:*) PREVIOUS_IMAGE_DIGEST="$PREVIOUS_IMAGE_ID" ;;
    *@sha256:*) PREVIOUS_IMAGE_DIGEST="${PREVIOUS_IMAGE_ID##*@}" ;;
    *) fail 'previous image identity is not immutable' ;;
  esac
  [[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'PREVIOUS_IMAGE_DIGEST format'

  printf 'traffic_preflight=%s\nprevious_revision=%s\nprevious_image_digest=%s\n' \
    "$TRAFFIC_PREFLIGHT_STATE" "$PREVIOUS_REVISION" "$PREVIOUS_IMAGE_DIGEST"
)
```

Record the exact output. The standard procedure must stop if this phase fails.

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

This repository has no tracked submodules, `.gitmodules`, or archive-altering
`.gitattributes` at this checkpoint. If any appears in the approved commit,
this phase stops rather than producing an incomplete archive.

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

  if [[ -e .gitmodules ]]; then fail 'submodules require a different build method'; fi
  if [[ -e .gitattributes ]]; then
    fail 'archive attributes require separate review'
  fi
  if ! SUBMODULE_PATHS="$(git ls-tree -r "$SOURCE_SHA" \
    | awk '$1 == "160000" {print $4}')"; then
    fail 'submodule inspection'
  fi
  [[ -z "$SUBMODULE_PATHS" ]] || fail 'tracked submodule found'

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
  TRACKED_LIST="${BUILD_ROOT}/tracked-files.txt"
  ARCHIVED_LIST="${BUILD_ROOT}/archived-files.txt"
  mkdir -- "$BUILD_CONTEXT" || fail 'context directory creation'

  git archive --format=tar --output="$ARCHIVE_PATH" "$SOURCE_SHA" \
    || fail 'git archive creation'
  if ! ARCHIVED_COMMIT="$(git get-tar-commit-id < "$ARCHIVE_PATH")"; then
    fail 'archive commit identification'
  fi
  [[ "$ARCHIVED_COMMIT" == "$SOURCE_SHA" ]] || fail 'archive commit mismatch'

  git ls-tree -r --name-only "$SOURCE_SHA" | LC_ALL=C sort > "$TRACKED_LIST" \
    || fail 'tracked file manifest'
  tar -tf "$ARCHIVE_PATH" | sed '/\/$/d' | LC_ALL=C sort > "$ARCHIVED_LIST" \
    || fail 'archive file manifest'
  cmp -s "$TRACKED_LIST" "$ARCHIVED_LIST" || fail 'archive differs from tracked tree'
  tar -xf "$ARCHIVE_PATH" -C "$BUILD_CONTEXT" || fail 'archive extraction'

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

The collision query returns names only. It must succeed, must include the known
`PREVIOUS_REVISION` as a coherence check, and must not include the proposed full
candidate name. Failure, empty output, or ambiguity stops deployment.

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

  EXISTING_REVISIONS=''
  if ! EXISTING_REVISIONS="$(gcloud run revisions list \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(metadata.name)')"; then
    fail 'revision collision query'
  fi
  [[ -n "$EXISTING_REVISIONS" ]] || fail 'collision query returned no evidence'
  grep -Fxq "$PREVIOUS_REVISION" <<< "$EXISTING_REVISIONS" \
    || fail 'known previous revision missing from collision output'
  if grep -Fxq "$CANDIDATE_REVISION" <<< "$EXISTING_REVISIONS"; then
    fail 'candidate revision already exists'
  fi

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
Copy all gates from the reviewed release record. A later increase requires a
new authorization and a new fresh phase after the approved observation criteria
and duration have passed.

```bash
(
  set -euo pipefail
  fail() { printf 'traffic movement failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  CANDIDATE_REVISION='REPLACE_WITH_RECORDED_CANDIDATE_REVISION'
  CANDIDATE_IMAGE_DIGEST='REPLACE_WITH_RECORDED_CANDIDATE_IMAGE_DIGEST'
  PREVIOUS_REVISION='REPLACE_WITH_RECORDED_PREVIOUS_REVISION'
  PREVIOUS_IMAGE_DIGEST='REPLACE_WITH_RECORDED_PREVIOUS_IMAGE_DIGEST'
  TRAFFIC_PREFLIGHT_STATE='REPLACE_WITH_APPROVED_PREFLIGHT_STATE'
  CANDIDATE_READY_GATE='REPLACE_WITH_APPROVED_READY_GATE'
  ZERO_TRAFFIC_GATE='REPLACE_WITH_APPROVED_ZERO_TRAFFIC_GATE'
  SMOKE_TEST_GATE='REPLACE_WITH_APPROVED_SMOKE_GATE'
  INTEGRATION_TEST_GATE='REPLACE_WITH_APPROVED_INTEGRATION_GATE'
  TRAFFIC_AUTHORIZATION='REPLACE_WITH_AUTHORIZATION_FOR_THIS_EXACT_CHANGE'
  APPROVED_PERCENT='REPLACE_WITH_APPROVED_CANDIDATE_PERCENT'
  REMAINDER_PERCENT='REPLACE_WITH_PREVIOUS_REVISION_PERCENT'

  for name in PROJECT_ID REGION SERVICE CANDIDATE_REVISION \
    CANDIDATE_IMAGE_DIGEST PREVIOUS_REVISION PREVIOUS_IMAGE_DIGEST \
    TRAFFIC_PREFLIGHT_STATE CANDIDATE_READY_GATE ZERO_TRAFFIC_GATE \
    SMOKE_TEST_GATE INTEGRATION_TEST_GATE TRAFFIC_AUTHORIZATION \
    APPROVED_PERCENT REMAINDER_PERCENT; do
    require_value "$name"
  done
  [[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'candidate digest format'
  [[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'previous digest format'
  [[ "$TRAFFIC_PREFLIGHT_STATE" == ONE_REVISION_AT_100_NO_TAGS ]] \
    || fail 'standard traffic precondition'
  [[ "$CANDIDATE_READY_GATE" == READY_TRUE_EXACT_REVISION_AND_DIGEST ]] \
    || fail 'Ready=True gate'
  [[ "$ZERO_TRAFFIC_GATE" == CANDIDATE_ZERO_PREVIOUS_100_NO_TAGS ]] \
    || fail 'zero-traffic gate'
  [[ "$SMOKE_TEST_GATE" == APPROVED_TEST_ONLY_SMOKE_EVIDENCE ]] \
    || fail 'smoke-test gate'
  [[ "$INTEGRATION_TEST_GATE" == APPROVED_TEST_ONLY_INTEGRATION_EVIDENCE ]] \
    || fail 'integration-test gate'
  [[ "$TRAFFIC_AUTHORIZATION" == APPROVED_FOR_THIS_EXACT_PERCENTAGE ]] \
    || fail 'traffic authorization'
  [[ "$APPROVED_PERCENT" =~ ^([1-9]|[1-9][0-9])$ ]] \
    || fail 'candidate percentage format'
  [[ "$REMAINDER_PERCENT" =~ ^([1-9]|[1-9][0-9])$ ]] \
    || fail 'previous percentage format'
  (( APPROVED_PERCENT < 100 )) || fail 'candidate percentage range'
  (( APPROVED_PERCENT + REMAINDER_PERCENT == 100 )) \
    || fail 'traffic percentages do not total 100'

  if ! gcloud run services update-traffic "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --to-revisions="${CANDIDATE_REVISION}=${APPROVED_PERCENT},${PREVIOUS_REVISION}=${REMAINDER_PERCENT}"; then
    fail 'authorized traffic update'
  fi
  gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='yaml(status.latestReadyRevisionName,status.traffic)' \
    || fail 'post-movement traffic query'
)
```

Stop after each movement for the approved observation period. Do not move 100%
through this gradual block; final cutover requires its own reviewed procedure.

## 9. Roll back to the verified pre-release state

This standard rollback is valid only because preflight proved that the original
state was one named revision at 100% with no tags. It restores that exact state,
not an arbitrarily selected older revision. A preexisting split requires its
separate approved full-map rollback plan instead.

```bash
(
  set -euo pipefail
  fail() { printf 'rollback failed: %s\n' "$1" >&2; exit 1; }
  require_value() {
    local name="$1" value="${!1-}"
    [[ -n "$value" && "$value" != REPLACE_* ]] || fail "$name is unresolved"
  }

  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  PREVIOUS_REVISION='REPLACE_WITH_RECORDED_PREVIOUS_REVISION'
  PREVIOUS_IMAGE_DIGEST='REPLACE_WITH_RECORDED_PREVIOUS_IMAGE_DIGEST'
  TRAFFIC_PREFLIGHT_STATE='REPLACE_WITH_APPROVED_PREFLIGHT_STATE'
  ROLLBACK_AUTHORIZATION='REPLACE_WITH_APPROVED_ROLLBACK_AUTHORIZATION'

  for name in PROJECT_ID REGION SERVICE PREVIOUS_REVISION PREVIOUS_IMAGE_DIGEST \
    TRAFFIC_PREFLIGHT_STATE ROLLBACK_AUTHORIZATION; do
    require_value "$name"
  done
  [[ "$TRAFFIC_PREFLIGHT_STATE" == ONE_REVISION_AT_100_NO_TAGS ]] \
    || fail 'standard rollback precondition'
  [[ "$PREVIOUS_REVISION" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]] \
    || fail 'PREVIOUS_REVISION format'
  [[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail 'recorded previous digest format'
  [[ "$ROLLBACK_AUTHORIZATION" == APPROVED_EXACT_PREVIOUS_REVISION_100 ]] \
    || fail 'rollback authorization'

  CURRENT_PREVIOUS_IMAGE_ID=''
  if ! CURRENT_PREVIOUS_IMAGE_ID="$(gcloud run revisions describe \
    "$PREVIOUS_REVISION" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='value(status.imageDigest)')"; then
    fail 'rollback-target digest query'
  fi
  [[ -n "$CURRENT_PREVIOUS_IMAGE_ID" ]] || fail 'rollback-target digest is empty'
  case "$CURRENT_PREVIOUS_IMAGE_ID" in
    sha256:*) CURRENT_PREVIOUS_DIGEST="$CURRENT_PREVIOUS_IMAGE_ID" ;;
    *@sha256:*) CURRENT_PREVIOUS_DIGEST="${CURRENT_PREVIOUS_IMAGE_ID##*@}" ;;
    *) fail 'rollback-target image is not immutable' ;;
  esac
  [[ "$CURRENT_PREVIOUS_DIGEST" == "$PREVIOUS_IMAGE_DIGEST" ]] \
    || fail 'rollback-target digest changed'

  if ! gcloud run services update-traffic "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --to-revisions="${PREVIOUS_REVISION}=100"; then
    fail 'rollback traffic update'
  fi
  gcloud run services describe "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format='yaml(status.latestReadyRevisionName,status.traffic)' \
    || fail 'post-rollback traffic query'
)
```

Verify the returned map is exactly the recorded pre-release state. Record the
reason, time, operator, impact, and follow-up decision. Preserve the candidate
revision and image for investigation unless a separate retention or security
authorization requires otherwise.

## 10. Remaining gates

Before deployment or participant enrollment, the owner must still authorize and
verify the gcloud command/output schemas, exact upload behavior, Linux/container
build, Artifact Registry permissions, Secret Manager references, required
runtime configuration, candidate smoke and integration behavior, Cloud Run
proxy/log privacy, Airtable/GHL compatibility, delivery, approved Gemini use,
browser and recovery behavior, monitoring, and rollback rehearsal.

Participant enrollment remains unauthorized.
