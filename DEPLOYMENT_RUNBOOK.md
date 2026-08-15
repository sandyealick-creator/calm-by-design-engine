# Controlled deployment provenance and rollback runbook

This is a future operator procedure. Correcting and testing this document does
not authorize or claim a build, deployment, live test, traffic change, rollback,
or participant enrollment. Each numbered Cloud phase requires a fresh, explicit
authorization bound to its exact account, project, region, service, revisions,
digests, and intended change.

The local validator is `scripts/validate_deployment_state.py`. It accepts strict
JSON on standard input, rejects duplicate object keys and malformed structures,
and never invokes `gcloud`, reads credentials, contacts a network, or mutates a
resource. Validation failures are stops; operators must not replace failed
evidence with manually interpreted YAML, flattened formatter output, approval
literals, or copied values.

## 1. Authorization boundaries and fixed identities

Record these fields before any separately authorized phase:

```text
ACCOUNT                 exact authorized user account
PROJECT_ID              exact project
PROJECT_NUMBER          exact project number verified with PROJECT_ID
BUILD_SERVICE_ACCOUNT   exact authorized Cloud Build service-account email
REGION                  exact Cloud Run and Artifact Registry region
SERVICE                 exact Cloud Run service
SOURCE_SHA              approved 40-character commit
SOURCE_TREE             approved 40-character Git tree
AR_REPOSITORY           exact Artifact Registry repository
IMAGE_NAME              exact image package
CANDIDATE_TAG            unique tag; normally SOURCE_SHA
CANDIDATE_REVISION       full intended revision name
CANDIDATE_SUFFIX         suffix that derives CANDIDATE_REVISION
BASELINE_REVISION        freshly verified fixed known-good revision
BASELINE_DIGEST          freshly verified immutable baseline digest
EXPECTED_ORIGIN          exact approved repository URL
EXPECTED_BRANCH          exact approved release branch
EXPECTED_ORIGIN_MAIN     exact approved local remote-tracking SHA
```

Build, candidate deployment, smoke testing, integration testing, every traffic
movement, rollback, and participant enrollment are independent authorization
boundaries. Evidence from one phase does not authorize the next.

The current Phase 2B baseline was `cbd-assess-00009-mkz` at
`sha256:6fd949d0e3ab3d4780f927088048009521ab8fb82f03253171e971862c31bcc3`,
but a future authorization must freshly verify it rather than trusting this
historical record.

Every executable Cloud evidence block below uses this strict local binder. It
adds the already-approved scope to one strict JSON response without interpreting
or printing it. Duplicate keys, malformed JSON, or a non-object response stop
the pipeline. The validator then requires the bound scope to equal its command
arguments.

In this procedure, a `*.raw.json` file preserves the exact standard-output bytes
emitted by the named local command. For `gcloud --format=json(...)`, those bytes
are gcloud's field-projected JSON output; they are not transport-level HTTP
response bytes. For `authorized_curl` with its output option, the file contains the selected
HTTP response-body bytes while the status is captured separately. Duplicate-key
detection begins when `bind_scope` or the validator parses those preserved bytes.
It detects duplicates present at that boundary, but cannot prove that an upstream
CLI did not already parse, normalize, or discard duplicate keys before emitting
its JSON. All downstream duplicate detection remains fail-closed.

```bash
bind_scope() {
  local payload_key="$1"
  python3.12 -c '
import json, sys
def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
try:
    evidence = json.load(sys.stdin, object_pairs_hook=pairs)
    if not isinstance(evidence, dict):
        raise ValueError("evidence is not an object")
    output = {
        "scope": {"project": sys.argv[2], "region": sys.argv[3], "service": sys.argv[4]},
        sys.argv[1]: evidence,
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
except Exception:
    print("scope binding failed", file=sys.stderr)
    raise SystemExit(2)
' "$payload_key" "$PROJECT_ID" "$REGION" "$SERVICE"
}

emit_bearer_config() {
  local access_token
  access_token="$(
    gcloud auth print-access-token --account="$ACCOUNT" --project="$PROJECT_ID" \
      | python3.12 -c '
import re, sys
token = sys.stdin.read().rstrip("\r\n")
if not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
    raise SystemExit(2)
sys.stdout.write(token)
'
  )" || return 1
  printf '%s\n' 'silent' 'show-error'
  printf 'header = "Authorization: Bearer %s"\n' "$access_token"
  unset access_token
}

authorized_curl() {
  local curl_config curl_status
  curl_config="$(emit_bearer_config)" || return 1
  curl --config <(printf '%s\n' "$curl_config") "$@"
  curl_status=$?
  unset curl_config
  return "$curl_status"
}
```

`emit_bearer_config` emits only fixed curl directives plus a locally validated
token character set. `authorized_curl` completes that validation before it
invokes curl, passes the transient configuration through a file descriptor, and
removes its shell variable afterward. URLs and output paths are separate quoted
curl arguments after validator-backed identifier and path checks; neither can
become another curl directive. The token is never placed in argv, an environment
variable, a persistent file, or evidence.

## 2. Local source preflight

This phase is local and performs no Cloud operation.

```bash
(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'source preflight failed: %s\n' "$1" >&2; exit 1; }

  SOURCE_SHA='REPLACE_WITH_APPROVED_FULL_SOURCE_SHA'
  SOURCE_TREE='REPLACE_WITH_APPROVED_FULL_SOURCE_TREE'
  EXPECTED_ORIGIN='https://github.com/sandyealick-creator/calm-by-design-engine.git'
  EXPECTED_BRANCH='REPLACE_WITH_APPROVED_RELEASE_BRANCH'
  EXPECTED_ORIGIN_MAIN='REPLACE_WITH_APPROVED_LOCAL_ORIGIN_MAIN_SHA'

  [[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'source SHA format'
  [[ "$SOURCE_TREE" =~ ^[0-9a-f]{40}$ ]] || fail 'source tree format'
  [[ "$(git remote)" == origin ]] || fail 'remote set differs from approval'
  [[ "$(git remote get-url --all origin | wc -l | tr -d " ")" == 1 ]] \
    || fail 'origin has multiple URLs'
  [[ "$(git remote get-url origin)" == "$EXPECTED_ORIGIN" ]] \
    || fail 'repository origin differs from approval'
  [[ "$(git branch --show-current)" == "$EXPECTED_BRANCH" ]] \
    || fail 'release branch differs from approval'
  [[ "$(git rev-parse refs/remotes/origin/main)" == "$EXPECTED_ORIGIN_MAIN" ]] \
    || fail 'local origin/main differs from approval; do not fetch here'
  [[ "$(git rev-parse HEAD)" == "$SOURCE_SHA" ]] || fail 'HEAD differs from source SHA'
  [[ "$(git rev-parse "${SOURCE_SHA}^{tree}")" == "$SOURCE_TREE" ]] \
    || fail 'source tree differs from approval'
  [[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]] \
    || fail 'worktree or index is not clean'
  git show -s --format='%H %T %s' "$SOURCE_SHA"
)
```

Record the empty status result, SHA, tree, and subject. A floating Cloud Run
`LATEST` target is not a standalone source-validation or candidate-image-build
blocker. It is handled before candidate deployment in Sections 6–8.

Before the first separately authorized Cloud operation, bind the shell to the
exact authorization and validate only the local gcloud identity context:

```bash
(
  set -euo pipefail
  fail() { printf 'identity preflight failed: %s\n' "$1" >&2; exit 1; }

  AUTHORIZED_ACCOUNT='REPLACE_WITH_EXACT_AUTHORIZED_ACCOUNT'
  AUTHORIZED_PROJECT='REPLACE_WITH_EXACT_AUTHORIZED_PROJECT'
  AUTHORIZED_REGION='REPLACE_WITH_EXACT_AUTHORIZED_REGION'
  AUTHORIZED_SERVICE='REPLACE_WITH_EXACT_AUTHORIZED_SERVICE'
  ACCOUNT="$AUTHORIZED_ACCOUNT"
  PROJECT_ID="$AUTHORIZED_PROJECT"
  REGION="$AUTHORIZED_REGION"
  SERVICE="$AUTHORIZED_SERVICE"

  ACTIVE_JSON="$(gcloud auth list --filter='status:ACTIVE' \
    --format='json(account,status)')" || fail 'active-account query'
  printf '%s' "$ACTIVE_JSON" | python3.12 -c '
import json, sys
expected = sys.argv[1]
def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result
try:
    data = json.load(sys.stdin, object_pairs_hook=pairs)
except Exception:
    raise SystemExit(2)
if data != [{"account": expected, "status": "ACTIVE"}]:
    raise SystemExit(2)
' "$AUTHORIZED_ACCOUNT" || fail 'active account is not the one authorized account'
  [[ "$(gcloud config get-value account)" == "$AUTHORIZED_ACCOUNT" ]] \
    || fail 'selected account mismatch'
  [[ "$(gcloud config get-value project)" == "$AUTHORIZED_PROJECT" ]] \
    || fail 'configured project mismatch'
  [[ "$REGION" == "$AUTHORIZED_REGION" && "$SERVICE" == "$AUTHORIZED_SERVICE" ]] \
    || fail 'region or service mismatch'
)
```

Any empty, plural, malformed, or different identity stops before a resource
query. Do not correct account or project selection inside a deployment phase.

The first authorized read-only resource query must bind the exact approved
project ID and project number from one authoritative metadata response. Neither
value may be derived from the other or from local configuration, service-account
names, or Secret Manager output:

```bash
{
  set -euo pipefail
  set -o noclobber
  fail() { printf 'project identity preflight failed: %s\n' "$1" >&2; exit 1; }
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  AUTHORIZED_PROJECT_NUMBER='REPLACE_WITH_EXACT_AUTHORIZED_PROJECT_NUMBER'
  PROJECT_METADATA_RAW="$EVIDENCE_ROOT/project-identity.raw.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$PROJECT_METADATA_RAW" \
    >/dev/null || fail 'unsafe or preexisting project evidence path'
  gcloud projects describe "$PROJECT_ID" \
    --account="$ACCOUNT" \
    --format='json(projectId,projectNumber,lifecycleState)' \
    > "$PROJECT_METADATA_RAW" || fail 'project metadata query'
  PROJECT_NUMBER="$(
    python3.12 scripts/validate_deployment_state.py project-identity \
      --project="$PROJECT_ID" --project-number="$AUTHORIZED_PROJECT_NUMBER" \
      --region="$REGION" --service="$SERVICE" --output=project-number \
      < "$PROJECT_METADATA_RAW"
  )" || fail 'project ID/number binding'
  [[ "$PROJECT_NUMBER" == "$AUTHORIZED_PROJECT_NUMBER" ]] \
    || fail 'project number differs from authorization'
}
```

Every Secret Manager validator command below requires that exact
`PROJECT_NUMBER`. A numeric resource segment is accepted only when it equals the
verified member of this pair; every other textual or numeric project is rejected.
Successful secret/version validation retains the observed resource identity and
matched project segment while emitting a consistent authorized canonical resource.

## 3. Strict traffic and baseline evidence

A separately authorized read-only preflight must capture the complete traffic
control map as strict JSON, including the exact latest-ready revision:

```bash
gcloud run services describe "$SERVICE" \
  --account="$ACCOUNT" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='json(status.latestReadyRevisionName,status.traffic)' \
  | bind_scope serviceState \
  | python3.12 scripts/validate_deployment_state.py traffic \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --latest-ready-revision="$BASELINE_REVISION"
```

The validator preserves fixed versus floating target type, revision identity,
percentage, and tag, while excluding service URLs from its output. It requires a
nonempty list totaling exactly 100 and rejects duplicate, null, malformed, or
unexpected targets.

For the exact baseline revision, obtain only identity, conditions, and digest:

```bash
gcloud run revisions describe "$BASELINE_REVISION" \
  --account="$ACCOUNT" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='json(metadata.name,status.conditions,status.imageDigest)' \
  | bind_scope revision \
  | python3.12 scripts/validate_deployment_state.py revision \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --expected-revision="$BASELINE_REVISION" \
      --expected-image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}" \
      --expected-digest="$BASELINE_DIGEST"
```

Exactly one `Ready=True` condition and the exact revision name are required.
The observed `status.imageDigest` must be one canonical digest-qualified image
reference whose registry host, project, repository, and image equal the
explicitly approved identity. Only after that full identity passes is its bare
lowercase `sha256:` digest compared with `BASELINE_DIGEST`. A bare observed
digest, tag-only image, wrong image identity, multiple `@` characters,
whitespace, uppercase, or malformed digest stops.
The `--expected-image` argument accepts only the exact tagless, digestless base
Artifact Registry image URI. Tag-qualified or digest-qualified arguments are
rejected rather than stripped or normalized.
Preserve the validator's command-output and resolved-effective map
representations in the release record.

## 4. Safe `SESSION_SECRET` and runtime-configuration gate

The real `SESSION_SECRET` reference remains unresolved. Repeating its query
requires separate authorization. Use the Cloud Run v2 `services.get` endpoint
with this partial-response selector only:

```text
template/containers/env/name,
template/containers/env/valueSource/secretKeyRef/secret,
template/containers/env/valueSource/secretKeyRef/version
```

Do not request `template/containers/env/value`. The future request must stream
the access token directly to the request mechanism and preserve the filtered HTTP
response-body bytes only in a unique, validator-approved evidence file before strict
validation. The token itself is never written:

```bash
(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'session reference gate failed: %s\n' "$1" >&2; exit 1; }
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  : "${SOURCE_SHA:?approved source SHA is required}"
  : "${PROJECT_NUMBER:?verified project number is required}"
  SESSION_RAW="$EVIDENCE_ROOT/${SOURCE_SHA}-session-reference.raw.json"
  SESSION_REFERENCE_RESULT_FILE="$EVIDENCE_ROOT/${SOURCE_SHA}-session-reference.result.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$SESSION_RAW" \
    --output-file="$SESSION_REFERENCE_RESULT_FILE" >/dev/null \
    || fail 'unsafe or preexisting evidence path'
  SESSION_URL="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/services/${SERVICE}?fields=template%2Fcontainers%2Fenv%2Fname%2Ctemplate%2Fcontainers%2Fenv%2FvalueSource%2FsecretKeyRef%2Fsecret%2Ctemplate%2Fcontainers%2Fenv%2FvalueSource%2FsecretKeyRef%2Fversion"
  authorized_curl --fail --url "$SESSION_URL" \
    > "$SESSION_RAW" || fail 'exact session reference request'
  bind_scope serviceConfig < "$SESSION_RAW" \
    | python3.12 scripts/validate_deployment_state.py session-secret \
        --project="$PROJECT_ID" --project-number="$PROJECT_NUMBER" \
        --region="$REGION" --service="$SERVICE" \
    > "$SESSION_REFERENCE_RESULT_FILE" || fail 'session reference validation'
)
```

The validator emits only the `SESSION_SECRET` name, referenced secret metadata,
and version selector. It rejects missing or duplicate entries, incomplete
references, nulls, malformed response shapes, and any supplied plaintext
`value` field. It never emits unrelated environment-variable names.

The reference validator accepts only a secret ID or full Secret Manager resource
and an exact positive numeric version. It rejects `latest`, aliases, whitespace,
control characters, URLs, assignments, and value-like strings. Save its
allowlisted result only in a preapproved metadata evidence directory. Then a
separately authorized phase may run this exact metadata-only construction. It
does not list secrets or versions and never accesses a payload. The saved
result is re-parsed with duplicate-key rejection, exact envelope and scope
matching, current-project secret-resource validation, and canonical positive
numeric version validation before either request URL is constructed:

```bash
(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'secret metadata gate failed: %s\n' "$1" >&2; exit 1; }
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  : "${SESSION_REFERENCE_RESULT_FILE:?validated reference result is required}"
  : "${PROJECT_NUMBER:?verified project number is required}"
  read -r SECRET_RESOURCE SECRET_VERSION < <(
    python3.12 scripts/validate_deployment_state.py secret-reference-result \
      --project="$PROJECT_ID" --project-number="$PROJECT_NUMBER" \
      --region="$REGION" --service="$SERVICE" \
      --evidence-root="$EVIDENCE_ROOT" \
      --input-file="$SESSION_REFERENCE_RESULT_FILE" \
      --output=resource-version
  ) || fail 'scope-bound reference result validation'
  SECRET_URL="https://secretmanager.googleapis.com/v1/${SECRET_RESOURCE}?fields=name"
  VERSION_RESOURCE="${SECRET_RESOURCE}/versions/${SECRET_VERSION}"
  VERSION_URL="https://secretmanager.googleapis.com/v1/${VERSION_RESOURCE}?fields=name%2Cstate"
  SECRET_BODY="$EVIDENCE_ROOT/secret-metadata-body.json"
  VERSION_BODY="$EVIDENCE_ROOT/secret-version-body.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$SECRET_BODY" \
    --output-file="$VERSION_BODY" >/dev/null \
    || fail 'unsafe or preexisting metadata evidence path'

  SECRET_STATUS="$(authorized_curl --url "$SECRET_URL" \
    --output "$SECRET_BODY" --write-out '%{http_code}')" \
    || fail 'exact secret metadata request'

  if [[ "$SECRET_STATUS" == 200 ]]; then
    VERSION_STATUS="$(authorized_curl --url "$VERSION_URL" \
      --output "$VERSION_BODY" --write-out '%{http_code}')" \
      || fail 'exact version metadata request'
  elif [[ "$SECRET_STATUS" == 404 ]]; then
    VERSION_STATUS=SKIPPED
  else
    fail 'secret query was neither exact success nor exact not-found'
  fi

  SECRET_ARGS=(
    secret-version --project="$PROJECT_ID" --project-number="$PROJECT_NUMBER"
    --region="$REGION" --service="$SERVICE"
    --expected-secret="$SECRET_RESOURCE" --expected-version="$SECRET_VERSION"
    --evidence-root="$EVIDENCE_ROOT" --secret-status="$SECRET_STATUS"
    --version-status="$VERSION_STATUS" --secret-evidence-file="$SECRET_BODY"
  )
  if [[ "$VERSION_STATUS" != SKIPPED ]]; then
    SECRET_ARGS+=(--version-evidence-file="$VERSION_BODY")
  fi
  python3.12 scripts/validate_deployment_state.py "${SECRET_ARGS[@]}"
)
```

Require `EXISTING_ENABLED`. Missing, disabled, destroyed, malformed,
permission-denied, ambiguous, or nonnumeric-version evidence stops deployment.

Before deployment, capture and approve only safe configuration metadata:

- environment-variable names and Secret Manager references, never plaintext
  values;
- runtime service account;
- CPU, memory, concurrency, and timeout;
- revision and service scaling;
- ingress and authentication policy;
- execution environment and networking;
- probes, ports, volumes, mounts, and other material template settings.

Installed gcloud implementation shows that omitted material flags do not create
corresponding configuration changes on an existing service. The candidate
command in Section 7 therefore inherits the existing service template except
for image and revision identity. That claim does not replace comparison. Before
and after deployment, run the same exact partial-response query below. It
excludes container image, revision identity, plaintext environment values,
service URLs, and payloads while retaining the approved runtime controls:

```bash
capture_runtime_snapshot() {
  local destination="$1"
  local runtime_fields
  local runtime_url
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$destination" >/dev/null \
    || return 1
  runtime_fields='name,ingress,invokerIamDisabled,iapEnabled'
  runtime_fields+=',scaling/manualInstanceCount,scaling/maxInstanceCount,scaling/minInstanceCount,scaling/scalingMode'
  runtime_fields+=',template/serviceAccount,template/maxInstanceRequestConcurrency,template/timeout,template/executionEnvironment'
  runtime_fields+=',template/scaling/minInstanceCount,template/scaling/maxInstanceCount,template/scaling/cpuUtilization,template/scaling/concurrencyUtilization'
  runtime_fields+=',template/vpcAccess/connector,template/vpcAccess/egress,template/vpcAccess/networkInterfaces/network,template/vpcAccess/networkInterfaces/subnetwork,template/vpcAccess/networkInterfaces/tags'
  runtime_fields+=',template/containers/name,template/containers/env/name,template/containers/env/valueSource/secretKeyRef/secret,template/containers/env/valueSource/secretKeyRef/version'
  runtime_fields+=',template/containers/resources/limits,template/containers/resources/cpuIdle,template/containers/resources/startupCpuBoost,template/containers/ports/name,template/containers/ports/containerPort'
  runtime_fields+=',template/containers/startupProbe/initialDelaySeconds,template/containers/startupProbe/timeoutSeconds,template/containers/startupProbe/periodSeconds,template/containers/startupProbe/failureThreshold,template/containers/startupProbe/httpGet/path,template/containers/startupProbe/httpGet/port,template/containers/startupProbe/httpGet/httpHeaders/name,template/containers/startupProbe/tcpSocket/port,template/containers/startupProbe/grpc/port,template/containers/startupProbe/grpc/service'
  runtime_fields+=',template/containers/livenessProbe/initialDelaySeconds,template/containers/livenessProbe/timeoutSeconds,template/containers/livenessProbe/periodSeconds,template/containers/livenessProbe/failureThreshold,template/containers/livenessProbe/httpGet/path,template/containers/livenessProbe/httpGet/port,template/containers/livenessProbe/httpGet/httpHeaders/name,template/containers/livenessProbe/tcpSocket/port,template/containers/livenessProbe/grpc/port,template/containers/livenessProbe/grpc/service'
  runtime_fields+=',template/containers/readinessProbe/initialDelaySeconds,template/containers/readinessProbe/timeoutSeconds,template/containers/readinessProbe/periodSeconds,template/containers/readinessProbe/failureThreshold,template/containers/readinessProbe/httpGet/path,template/containers/readinessProbe/httpGet/port,template/containers/readinessProbe/httpGet/httpHeaders/name,template/containers/readinessProbe/tcpSocket/port,template/containers/readinessProbe/grpc/port,template/containers/readinessProbe/grpc/service'
  runtime_fields+=',template/containers/volumeMounts/name,template/containers/volumeMounts/mountPath,template/containers/volumeMounts/subPath'
  runtime_fields+=',template/volumes/name,template/volumes/cloudSqlInstance/instances,template/volumes/emptyDir/medium,template/volumes/emptyDir/sizeLimit,template/volumes/gcs/bucket,template/volumes/gcs/mountOptions,template/volumes/gcs/readOnly,template/volumes/nfs/server,template/volumes/nfs/path,template/volumes/nfs/readOnly,template/volumes/secret/secret,template/volumes/secret/defaultMode,template/volumes/secret/items/path,template/volumes/secret/items/version,template/volumes/secret/items/mode'
  runtime_url="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/services/${SERVICE}?fields=${runtime_fields}"
  authorized_curl --fail --url "$runtime_url" \
    > "$destination" || return 1
  bind_scope serviceConfig < "$destination" \
    | python3.12 scripts/validate_deployment_state.py runtime-snapshot \
        --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    >/dev/null
}

```

Section 7 invokes this function once immediately before and once immediately
after deployment, then passes both strictly parsed HTTP response bodies to `runtime-equal`.
Require `RUNTIME_UNCHANGED`. The validator requires a nonempty container list.
A singleton container may omit `name`; it is compared by its sole position, and
name presence or absence is not drift. A present singleton name must be valid.
Multiple containers require explicit, valid, unique names and are compared by
name independent of order. The validator validates every selected service,
template, container, environment,
secret-reference, resource, port, probe, volume, scaling, and networking field
against its Cloud Run v2 shape. This includes distinct service-level scaling,
revision-level scaling, and startup, liveness, and readiness probes. Optional
selected fields may be absent, but a present field with the wrong type or
structure stops. The validator enforces the one-port and one-network-interface
limits, the documented CPU and concurrency utilization ranges, and the rule that
both utilization thresholds cannot be disabled together. Plaintext environment
values and probe-header values are deliberately not requested; only probe-header
names are compared. The result proves equality of this exact safe projection,
not of unselected values. Wrong service identity, malformed or truncated
projection, unknown key, or hash difference stops. Both complete raw documents
are revalidated before equality can be classified; their retention or deletion
follows the separately approved evidence policy.

## 5. Separately authorized immutable candidate build

Historical Container Analysis provenance and SLSA level may remain unavailable.
Do not enable Container Analysis. Capture new candidate evidence directly.

Before building, execute the exact tag-resource GET below. It requests only the
resource name. The validator accepts only a structurally exact HTTP 404 with API
status `NOT_FOUND`; HTTP 200 is a collision, 401/403 is permission denial, and
every malformed, empty, plural, or other result stops. It never lists tags.

```bash
(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'candidate build failed: %s\n' "$1" >&2; exit 1; }

  SOURCE_SHA='REPLACE_WITH_APPROVED_SOURCE_SHA'
  SOURCE_TREE='REPLACE_WITH_APPROVED_SOURCE_TREE'
  ACCOUNT='REPLACE_WITH_AUTHORIZED_ACCOUNT'
  PROJECT_ID='REPLACE_WITH_PROJECT_ID'
  PROJECT_NUMBER='REPLACE_WITH_VERIFIED_PROJECT_NUMBER'
  BUILD_SERVICE_ACCOUNT='REPLACE_WITH_AUTHORIZED_BUILD_SERVICE_ACCOUNT_EMAIL'
  REGION='REPLACE_WITH_REGION'
  SERVICE='REPLACE_WITH_SERVICE'
  AR_REPOSITORY='REPLACE_WITH_REPOSITORY'
  IMAGE_NAME='REPLACE_WITH_IMAGE_NAME'
  CANDIDATE_TAG="$SOURCE_SHA"
  IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}"
  CANDIDATE_IMAGE_TAG="${IMAGE_URI}:${CANDIDATE_TAG}"
  TAG_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/repositories/${AR_REPOSITORY}/packages/${IMAGE_NAME}/tags/${CANDIDATE_TAG}"
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  declare -F bind_scope >/dev/null || fail 'bind_scope is not loaded'

  [[ "$(git rev-parse HEAD)" == "$SOURCE_SHA" ]] || fail 'HEAD mismatch'
  [[ "$(git rev-parse "${SOURCE_SHA}^{tree}")" == "$SOURCE_TREE" ]] \
    || fail 'tree mismatch'
  [[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]] \
    || fail 'worktree or index is not clean'

  TAG_COLLISION_BODY="$EVIDENCE_ROOT/candidate-tag-preflight.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$TAG_COLLISION_BODY" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" >/dev/null \
    || fail 'unsafe build identity or candidate-tag evidence path'
  TAG_URL="https://artifactregistry.googleapis.com/v1/${TAG_RESOURCE}?fields=name"
  TAG_STATUS="$(authorized_curl --url "$TAG_URL" \
    --output "$TAG_COLLISION_BODY" --write-out '%{http_code}')" \
    || fail 'candidate tag GET failed'
  python3.12 scripts/validate_deployment_state.py nonexistence \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --kind=CANDIDATE_TAG --expected-resource="$TAG_RESOURCE" \
      --http-status="$TAG_STATUS" < "$TAG_COLLISION_BODY"

  TMP_BASE="${TMPDIR:-/tmp}"
  [[ "$TMP_BASE" == /* && "$TMP_BASE" != / && -d "$TMP_BASE" && -w "$TMP_BASE" ]] \
    || fail 'temporary base is unsafe'
  BUILD_ROOT="$(mktemp -d "${TMP_BASE%/}/cbd-build.${SOURCE_SHA}.XXXXXXXX")" \
    || fail 'temporary context creation'
  [[ "$BUILD_ROOT" == "${TMP_BASE%/}/cbd-build.${SOURCE_SHA}."* \
    && -d "$BUILD_ROOT" ]] || fail 'temporary context path'
  mkdir -- "$BUILD_ROOT/context" || fail 'context directory creation'
  git archive --format=tar --output="$BUILD_ROOT/source.tar" "$SOURCE_SHA"
  [[ "$(git get-tar-commit-id < "$BUILD_ROOT/source.tar")" == "$SOURCE_SHA" ]] \
    || fail 'archive commit mismatch'

  python3.12 - "$SOURCE_SHA" "$BUILD_ROOT/source.tar" "$BUILD_ROOT/context" <<'PY' \
    || fail 'exact tree/archive validation'
import fnmatch, os, re, subprocess, sys, tarfile
from pathlib import Path, PurePosixPath

source_sha, archive_path, context_path = sys.argv[1:]
context = Path(context_path)
def stop():
    raise SystemExit(2)
if not re.fullmatch(r"[0-9a-f]{40}", source_sha) or not context.is_absolute() \
        or not context.is_dir() or any(context.iterdir()):
    stop()
tree = subprocess.run(
    ["git", "ls-tree", "-rz", "--full-tree", source_sha],
    check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
).stdout
records = tree.split(b"\0")
if not records or records[-1] != b"":
    stop()
records.pop()
tracked = {}
sensitive = (
    ".env", "*.env", "env.yaml", "*credentials*.json", "service-account*.json",
    "*-key.json", "*.pem", "*.p12", "*.log", "*_export.csv", "*_export.json",
    "logs/*", "participant_data/*", "scratch/*", "tmp/*",
)
for record in records:
    try:
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid = (part.decode("ascii") for part in header.split(b" ", 2))
        path = raw_path.decode("utf-8", "strict")
    except Exception:
        stop()
    parts = path.split("/")
    if kind != "blob" or mode not in {"100644", "100755"} \
            or not re.fullmatch(r"[0-9a-f]{40}", oid) or path in tracked \
            or path.startswith("/") or path.endswith("/") or "//" in path \
            or any(part in {"", ".", ".."} for part in parts) \
            or PurePosixPath(path).as_posix() != path \
            or any(ord(char) < 32 or ord(char) == 127 for char in path) \
            or parts[-1] in {".gitmodules", ".gitattributes"} \
            or any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(parts[-1], pattern)
                   for pattern in sensitive):
        stop()
    tracked[path] = (mode, oid)
if not {"Dockerfile", "requirements.txt"}.issubset(tracked):
    stop()
directories = {
    "/".join(path.split("/")[:index])
    for path in tracked for index in range(1, len(path.split("/")))
}
with tarfile.open(archive_path, "r:") as archive:
    files, archive_directories = {}, set()
    for member in archive.getmembers():
        name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
        if member.isdir() and name in directories:
            archive_directories.add(name)
        elif member.isfile() and name in tracked and name not in files:
            files[name] = member
        else:
            stop()
    if set(files) != set(tracked) or archive_directories != directories:
        stop()
    for path, (mode, oid) in tracked.items():
        member = files[path]
        if bool(member.mode & 0o111) != (mode == "100755") or member.mode & 0o7000:
            stop()
        source = archive.extractfile(member)
        blob = subprocess.run(
            ["git", "cat-file", "blob", oid], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ).stdout
        if source is None or source.read() != blob:
            stop()
    for directory in sorted(directories, key=lambda item: (item.count("/"), item)):
        (context / directory).mkdir()
    for path, (mode, _oid) in tracked.items():
        source = archive.extractfile(files[path])
        if source is None:
            stop()
        destination = context.joinpath(*path.split("/"))
        with destination.open("xb") as output:
            output.write(source.read())
        os.chmod(destination, 0o755 if mode == "100755" else 0o644)
print(f"validated_tree_files={len(tracked)}")
PY

  BUILD_CONFIG="$EVIDENCE_ROOT/${SOURCE_SHA}-cloudbuild.json"
  BUILD_CONFIG_DIGEST_FILE="$EVIDENCE_ROOT/${SOURCE_SHA}-cloudbuild.sha256"
  BUILD_CONFIG_RESULT_FILE="$EVIDENCE_ROOT/${SOURCE_SHA}-cloudbuild.validated.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$BUILD_CONFIG" \
    --output-file="$BUILD_CONFIG_DIGEST_FILE" \
    --output-file="$BUILD_CONFIG_RESULT_FILE" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" >/dev/null \
    || fail 'unsafe or preexisting build-config evidence path'
  python3.12 - "$BUILD_CONFIG" <<'PY' \
    || fail 'deterministic build-config construction'
import json, sys
from pathlib import Path

config = {
    "steps": [{
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
    }],
    "images": ["${_CANDIDATE_IMAGE}"],
    "options": {"substitutionOption": "MUST_MATCH"},
}
with Path(sys.argv[1]).open("x", encoding="utf-8", newline="\n") as output:
    output.write(json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n")
PY
  BUILD_CONFIG_SHA256="$(
    python3.12 scripts/validate_deployment_state.py build-config \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --expected-source-sha="$SOURCE_SHA" \
      --expected-source-tree="$SOURCE_TREE" \
      --expected-image-tag="$CANDIDATE_IMAGE_TAG" --output=sha256 \
      < "$BUILD_CONFIG"
  )" || fail 'strict explicit build-config validation'
  printf '%s\n' "$BUILD_CONFIG_SHA256" > "$BUILD_CONFIG_DIGEST_FILE"
  python3.12 scripts/validate_deployment_state.py build-config \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --expected-source-sha="$SOURCE_SHA" \
    --expected-source-tree="$SOURCE_TREE" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" \
    < "$BUILD_CONFIG" > "$BUILD_CONFIG_RESULT_FILE" \
    || fail 'build-config evidence capture'

  BUILD_SUBMISSION_RAW="$EVIDENCE_ROOT/${SOURCE_SHA}-build-submission.raw.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$BUILD_SUBMISSION_RAW" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" >/dev/null \
    || fail 'unsafe or preexisting build-submission evidence path'
  gcloud builds submit "$BUILD_ROOT/context" \
    --account="$ACCOUNT" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --config="$BUILD_CONFIG" \
    --substitutions="_SOURCE_SHA=${SOURCE_SHA},_SOURCE_TREE=${SOURCE_TREE},_CANDIDATE_IMAGE=${CANDIDATE_IMAGE_TAG}" \
    --async \
    --format='json(id)' > "$BUILD_SUBMISSION_RAW" || fail 'build submission'
  BUILD_ID="$(bind_scope buildSubmission < "$BUILD_SUBMISSION_RAW" \
    | python3.12 scripts/validate_deployment_state.py build-submission \
        --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
        --output=build-id)" || fail 'strict build identifier capture'

  BUILD_COMPLETE=false
  BUILD_SUCCESS_RAW_FILE=''
  for ((poll=1; poll<=90; poll++)); do
    BUILD_POLL_RAW="$EVIDENCE_ROOT/${BUILD_ID}-poll-${poll}.raw.json"
    python3.12 scripts/validate_deployment_state.py evidence-path \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --evidence-root="$EVIDENCE_ROOT" --output-file="$BUILD_POLL_RAW" \
      --expected-image-tag="$CANDIDATE_IMAGE_TAG" >/dev/null \
      || fail 'unsafe or preexisting build-poll evidence path'
    set +e
    BUILD_URL="https://cloudbuild.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/builds/${BUILD_ID}"
    BUILD_FIELDS='name,id,projectId,status,serviceAccount,steps(name,args),images,options(substitutionOption),substitutions,source(storageSource(bucket,object,generation)),sourceProvenance(resolvedStorageSource(bucket,object,generation)),createTime,startTime,finishTime,results(images(name,digest,artifactRegistryPackage,ociMediaType,pushTiming(startTime,endTime)))'
    authorized_curl --fail --get --url "$BUILD_URL" \
      --data-urlencode "fields=$BUILD_FIELDS" > "$BUILD_POLL_RAW"
    describe_rc=$?
    if [[ "$describe_rc" == 0 ]]; then
      python3.12 scripts/validate_deployment_state.py build --raw \
          --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
          --expected-build-id="$BUILD_ID" --expected-source-sha="$SOURCE_SHA" \
          --expected-source-tree="$SOURCE_TREE" \
          --expected-image-tag="$CANDIDATE_IMAGE_TAG" \
          --project-number="$PROJECT_NUMBER" \
          --expected-service-account="$BUILD_SERVICE_ACCOUNT" \
          --evidence-root="$EVIDENCE_ROOT" --build-config-file="$BUILD_CONFIG" \
          --expected-build-config-sha256="$BUILD_CONFIG_SHA256" \
          < "$BUILD_POLL_RAW" >/dev/null
      build_rc=$?
    else
      build_rc=2
    fi
    set -e
    case "$build_rc" in
      0) BUILD_COMPLETE=true; BUILD_SUCCESS_RAW_FILE="$BUILD_POLL_RAW"; break ;;
      3) sleep 10 ;;
      *) fail 'build failed, was malformed, or reached a non-success state' ;;
    esac
  done
  [[ "$BUILD_COMPLETE" == true ]] || fail 'build did not complete within 900 seconds'

  BUILD_IMAGE_DIGEST="$(
    python3.12 scripts/validate_deployment_state.py build --raw \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --expected-build-id="$BUILD_ID" --expected-source-sha="$SOURCE_SHA" \
      --expected-source-tree="$SOURCE_TREE" \
      --expected-image-tag="$CANDIDATE_IMAGE_TAG" \
      --project-number="$PROJECT_NUMBER" \
      --expected-service-account="$BUILD_SERVICE_ACCOUNT" \
      --evidence-root="$EVIDENCE_ROOT" --build-config-file="$BUILD_CONFIG" \
      --expected-build-config-sha256="$BUILD_CONFIG_SHA256" --output=digest \
      < "$BUILD_SUCCESS_RAW_FILE"
  )" || fail 'canonical build digest extraction'

  TAG_RESULT_BODY="$EVIDENCE_ROOT/${BUILD_ID}-docker-image.raw.json"
  IMAGE_AUTHORIZATION_FILE="$EVIDENCE_ROOT/${BUILD_ID}-image-authorization.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$TAG_RESULT_BODY" \
    --output-file="$IMAGE_AUTHORIZATION_FILE" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" >/dev/null \
    || fail 'unsafe or preexisting image evidence path'
  DOCKER_IMAGE_URL="$(
    python3.12 scripts/validate_deployment_state.py artifact-image-request \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --expected-image-tag="$CANDIDATE_IMAGE_TAG" \
      --expected-digest="$BUILD_IMAGE_DIGEST" --output=url
  )" || fail 'exact candidate DockerImage request construction'
  authorized_curl --fail --url "$DOCKER_IMAGE_URL" \
    > "$TAG_RESULT_BODY" || fail 'exact candidate DockerImage GET'
  python3.12 scripts/validate_deployment_state.py tag-resolution --raw \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --project-number="$PROJECT_NUMBER" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" < "$TAG_RESULT_BODY" >/dev/null \
    || fail 'candidate DockerImage validation'
  python3.12 scripts/validate_deployment_state.py authorize-image \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" \
    --build-evidence-file="$BUILD_SUCCESS_RAW_FILE" \
    --tag-evidence-file="$TAG_RESULT_BODY" --expected-build-id="$BUILD_ID" \
    --expected-source-sha="$SOURCE_SHA" --expected-source-tree="$SOURCE_TREE" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" \
    --project-number="$PROJECT_NUMBER" \
    --expected-service-account="$BUILD_SERVICE_ACCOUNT" \
    --build-config-file="$BUILD_CONFIG" \
    --expected-build-config-sha256="$BUILD_CONFIG_SHA256" \
    > "$IMAGE_AUTHORIZATION_FILE" \
    || fail 'three-way image digest authorization'

  printf 'validated_build_context=%s\n' "$BUILD_ROOT/context"
)
```

The deterministic explicit JSON build configuration is created in the evidence
directory, outside the submitted source context. Its exact bytes and SHA-256 are
captured before submission. It is evidence, not application source. The one
Docker step consumes `_SOURCE_SHA`, `_SOURCE_TREE`, and `_CANDIDATE_IMAGE` under
explicit `MUST_MATCH`; `ALLOW_LOOSE` is prohibited. `--config` and `--tag` must
never be combined. The independently validated Git archive remains the source
identity and is not altered by the evidence-only build configuration.

Exit 0 requires the exact tag to have been absent, a byte-exact safe Git context,
one exact validated submitted build configuration, one valid build ID, `SUCCESS`
within the bounded polling window, an exact returned Build resource with the
verified project-ID/project-number alias pair, authorized service account,
resolved Docker arguments, exact source/resolved-source identity, substitutions,
and one resolved `images[]` value, and
exactly one matching `results.images[]` BuiltImage with its
canonical digest, exact Artifact Registry Package-version resource ending in
that digest, valid optional OCI
media type, and—when present—a `pushTiming` TimeSpan fully bounded by the Build
execution interval at nanosecond precision:
`createTime <= startTime <= pushStart <= pushEnd <= finishTime`. The exact
Artifact Registry v1 DockerImage resource name is constructed only from the
validated candidate tag and Build digest and retrieved with one `DockerImages.Get`.
The local gcloud 578.0.0 generated client defines that request as `GET
v1/{+name}` with no query or pagination parameters and a single `DockerImage`
response; the generated message defines the requested `name`, `uri`, and `tags`
fields. The independently queried DockerImage must identify that same project,
location, repository, image, bare source-SHA tag component, and digest.
`Build.options.substitutionOption` may be absent only as Cloud Build's default
zero-value `MUST_MATCH`, and only because the separately hash-bound submitted
config explicitly requires `MUST_MATCH`; explicit `ALLOW_LOOSE` always fails.
Raw REST bytes are preserved before strict validation, and nested repeated
fields use the proven REST selector rather than dotted CLI projection.
`authorize-image` requires equality
of the Build-result digest, DockerImage URI digest, and final digest-qualified
image reference. This is
Build artifact-output binding, not a SLSA attestation. Every queued or working
state is retry-only; cancelled, expired, timed out, failed, unknown, empty, or
contradictory evidence stops.

There is deliberately no cleanup trap or deletion command. Record `BUILD_ROOT`.
Retention or deletion of that exact directory requires its own reviewed action.
The release-record destination must be approved before execution. No build is
authorized by this document.

## 6. Exact candidate-revision collision gate

Derive and validate the intended name locally:

```bash
[[ "$CANDIDATE_REVISION" == "${SERVICE}-${CANDIDATE_SUFFIX}" ]]
[[ "$CANDIDATE_REVISION" =~ ^[a-z]([a-z0-9-]*[a-z0-9])?$ ]]
(( ${#CANDIDATE_REVISION} <= 63 ))
```

Then execute this exact Cloud Run v2 revision-resource GET. It requests only
`name`; no revision listing is permitted:

```bash
(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'candidate revision collision gate failed: %s\n' "$1" >&2; exit 1; }
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  REVISION_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/services/${SERVICE}/revisions/${CANDIDATE_REVISION}"
  REVISION_BODY="$EVIDENCE_ROOT/candidate-revision-preflight.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$REVISION_BODY" >/dev/null \
    || fail 'unsafe or preexisting revision evidence path'
  REVISION_URL="https://run.googleapis.com/v2/${REVISION_RESOURCE}?fields=name"
  REVISION_STATUS="$(authorized_curl --url "$REVISION_URL" \
    --output "$REVISION_BODY" --write-out '%{http_code}')" \
    || fail 'exact candidate revision GET'
  python3.12 scripts/validate_deployment_state.py nonexistence \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --kind=CANDIDATE_REVISION --expected-resource="$REVISION_RESOURCE" \
      --http-status="$REVISION_STATUS" < "$REVISION_BODY"
)
```

Only `CANDIDATE_REVISION_AVAILABLE` permits the separately authorized deploy.
Exact success is a collision; permission denial, malformed output, wrong
identity, an unexpected status, or command failure stops.

## 7. Separately authorized zero-traffic candidate deployment

After separate zero-traffic deployment authorization, run this block. It
requires the exact successful REST Build and DockerImage evidence, revalidates
their complete current envelope and digest equality, captures pre-state before
mutation, validates the exact candidate revision afterward, proves the candidate
is the latest created revision, validates the complete fixed production traffic
map, and compares runtime hashes. A temporary revision-tag history may leave
`latestReadyRevisionName` at the independently validated candidate even though
the fixed production target remains the baseline:

```bash
(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'zero-traffic deployment gate failed: %s\n' "$1" >&2; exit 1; }
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  : "${AUTHORIZED_BUILD_EVIDENCE_FILE:?authorized successful Build evidence is required}"
  : "${AUTHORIZED_TAG_EVIDENCE_FILE:?authorized DockerImage evidence is required}"
  : "${BUILD_ID:?authorized build ID is required}"
  : "${SOURCE_SHA:?authorized source SHA is required}"
  : "${SOURCE_TREE:?authorized source tree is required}"
  : "${CANDIDATE_IMAGE_TAG:?authorized candidate image tag is required}"
  : "${PROJECT_NUMBER:?verified project number is required}"
  : "${BUILD_SERVICE_ACCOUNT:?authorized Build service account is required}"
  : "${BUILD_CONFIG:?authorized submitted build config is required}"
  : "${BUILD_CONFIG_SHA256:?authorized submitted build config digest is required}"
  : "${PRE_APPROVED_LATEST_READY_REVISION:?authorized pre-deployment latest-ready revision is required}"
  declare -F bind_scope >/dev/null || fail 'bind_scope is not loaded'
  declare -F capture_runtime_snapshot >/dev/null \
    || fail 'capture_runtime_snapshot is not loaded'

  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" \
    --input-file="$AUTHORIZED_BUILD_EVIDENCE_FILE" \
    --input-file="$AUTHORIZED_TAG_EVIDENCE_FILE" \
    --expected-image-tag="$CANDIDATE_IMAGE_TAG" >/dev/null \
    || fail 'stale, cross-scope, or unsafe image evidence paths'
  CANDIDATE_IMAGE_DIGEST_REF="$(
    python3.12 scripts/validate_deployment_state.py authorize-image \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --evidence-root="$EVIDENCE_ROOT" \
      --build-evidence-file="$AUTHORIZED_BUILD_EVIDENCE_FILE" \
      --tag-evidence-file="$AUTHORIZED_TAG_EVIDENCE_FILE" \
      --expected-build-id="$BUILD_ID" --expected-source-sha="$SOURCE_SHA" \
      --expected-source-tree="$SOURCE_TREE" \
      --expected-image-tag="$CANDIDATE_IMAGE_TAG" \
      --project-number="$PROJECT_NUMBER" \
      --expected-service-account="$BUILD_SERVICE_ACCOUNT" \
      --build-config-file="$BUILD_CONFIG" \
      --expected-build-config-sha256="$BUILD_CONFIG_SHA256" --output=image-ref
  )" || fail 'current build/tag/digest authorization'

  PRE_APPROVED_LATEST_READY_ARGS=()
  if [[ "$PRE_APPROVED_LATEST_READY_REVISION" != "$BASELINE_REVISION" ]]; then
    : "${PRE_APPROVED_LATEST_READY_DIGEST:?approved pre-deployment revision digest is required}"
    : "${PRE_APPROVED_LATEST_READY_IMAGE:?approved pre-deployment revision image is required}"
    PRE_APPROVED_LATEST_READY_RAW="$EVIDENCE_ROOT/pre-approved-latest-ready-revision.json"
    python3.12 scripts/validate_deployment_state.py evidence-path \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --evidence-root="$EVIDENCE_ROOT" \
      --output-file="$PRE_APPROVED_LATEST_READY_RAW" >/dev/null \
      || fail 'unsafe pre-approved latest-ready evidence path'
    gcloud run revisions describe "$PRE_APPROVED_LATEST_READY_REVISION" \
      --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
      --format='json(metadata.name,status.conditions,status.imageDigest)' \
      > "$PRE_APPROVED_LATEST_READY_RAW"
    bind_scope revision < "$PRE_APPROVED_LATEST_READY_RAW" \
      | python3.12 scripts/validate_deployment_state.py revision \
          --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
          --expected-revision="$PRE_APPROVED_LATEST_READY_REVISION" \
          --expected-image="$PRE_APPROVED_LATEST_READY_IMAGE" \
          --expected-digest="$PRE_APPROVED_LATEST_READY_DIGEST" >/dev/null \
      || fail 'pre-approved latest-ready revision evidence is not ready or exact'
    PRE_APPROVED_LATEST_READY_ARGS=(
      --pre-approved-latest-ready-evidence-file "$PRE_APPROVED_LATEST_READY_RAW"
      --pre-approved-latest-ready-digest "$PRE_APPROVED_LATEST_READY_DIGEST"
      --pre-approved-latest-ready-image "$PRE_APPROVED_LATEST_READY_IMAGE"
    )
  fi

  PRE_SERVICE_RAW="$EVIDENCE_ROOT/pre-deploy-traffic.json"
  POST_SERVICE_RAW="$EVIDENCE_ROOT/post-deploy-traffic.json"
  PRE_RUNTIME_RAW="$EVIDENCE_ROOT/pre-runtime.raw.json"
  POST_RUNTIME_RAW="$EVIDENCE_ROOT/post-runtime.raw.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$PRE_SERVICE_RAW" \
    --output-file="$POST_SERVICE_RAW" --output-file="$PRE_RUNTIME_RAW" \
    --output-file="$POST_RUNTIME_RAW" >/dev/null \
    || fail 'unsafe or preexisting deployment evidence path'

  gcloud run services describe "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(status.latestReadyRevisionName,status.traffic)' \
    > "$PRE_SERVICE_RAW"
  bind_scope serviceState < "$PRE_SERVICE_RAW" \
    | python3.12 scripts/validate_deployment_state.py traffic \
        --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
        --latest-ready-revision="$PRE_APPROVED_LATEST_READY_REVISION" >/dev/null
  capture_runtime_snapshot "$PRE_RUNTIME_RAW"

  gcloud run deploy "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --image="$CANDIDATE_IMAGE_DIGEST_REF" \
    --revision-suffix="$CANDIDATE_SUFFIX" --no-traffic --quiet

  gcloud run revisions describe "$CANDIDATE_REVISION" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(metadata.name,status.conditions,status.imageDigest)' \
    | bind_scope revision \
    | python3.12 scripts/validate_deployment_state.py revision \
        --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
        --expected-revision="$CANDIDATE_REVISION" \
        --expected-image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}" \
        --expected-digest="${CANDIDATE_IMAGE_DIGEST_REF##*@}"

  gcloud run services describe "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(status.latestCreatedRevisionName,status.latestReadyRevisionName,status.traffic)' \
    > "$POST_SERVICE_RAW"
  bind_scope serviceState < "$POST_SERVICE_RAW" \
    | python3.12 scripts/validate_deployment_state.py traffic \
        --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
        --latest-ready-revision="$BASELINE_REVISION" >/dev/null

  python3.12 scripts/validate_deployment_state.py zero-traffic \
      --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
      --candidate-revision="$CANDIDATE_REVISION" \
      --baseline-revision="$BASELINE_REVISION" \
      --pre-latest-ready-revision="$PRE_APPROVED_LATEST_READY_REVISION" \
      "${PRE_APPROVED_LATEST_READY_ARGS[@]}" \
      --evidence-root="$EVIDENCE_ROOT" --pre-evidence-file="$PRE_SERVICE_RAW" \
      --post-evidence-file="$POST_SERVICE_RAW"

  capture_runtime_snapshot "$POST_RUNTIME_RAW"
  python3.12 scripts/validate_deployment_state.py runtime-equal \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" \
    --pre-evidence-file="$PRE_RUNTIME_RAW" \
    --post-evidence-file="$POST_RUNTIME_RAW"
)
```

`CANDIDATE_IMAGE_DIGEST_REF` must contain `@sha256:` and must not use an image
tag or implicit latest image. Do not add environment, secret, IAM, ingress,
service-account, scaling, CPU, memory, concurrency, timeout, networking, probe,
or volume flags unless a new review explicitly authorizes a complete restatement
of configuration.

Exit 0 requires exact candidate name, digest, and `Ready=True`; observed
post-deployment `latestCreatedRevisionName` equal to the candidate; and
`latestReadyRevisionName` equal to either the fixed baseline or the
independently validated candidate. The complete fixed, untagged traffic map is
authoritative for production serving state: baseline at 100%, candidate absent
or untagged at zero, and no floating `LATEST` or unexpected target. It also
requires identical safe runtime hashes. A correct `--no-traffic` deployment
therefore has a newest-created, independently ready candidate and baseline
production traffic at 100% with candidate traffic at 0%, regardless of the
allowed latest-ready identity.

Before executing the block, authorization must bind
`PRE_APPROVED_LATEST_READY_REVISION` to either the fixed baseline or a
separately captured approved candidate. When it is not the baseline, the block
captures a fresh exact revision document and binds its identity, `Ready=True`,
image identity, and immutable digest to the approved values before deployment.
The complete fixed `status.traffic` map remains the sole production-serving
proof: the baseline must be exactly 100 percent, the new candidate must be
absent or zero percent, and tags, floating `LATEST`, and unexpected targets
fail both before and after deployment.

The pre- and post-deployment traffic maps require fixed revision targets. Do
not accept a floating-to-fixed transformation. After omitting only the allowed
explicit zero-traffic entry for the new candidate, the fixed target inventory
and allocation must be unchanged. Any traffic drift, candidate traffic,
candidate tag, unexpected target, missing target, changed tag, or changed
percentage is a deployment failure. Do not issue a compensating traffic command
without a separate rollback authorization.

## 8. Separately authorized candidate testing

Smoke and integration testing are independent authorizations and must use only
owner-controlled test contacts and records. Before testing, require exact
candidate readiness, digest, zero-traffic evidence, runtime configuration
preservation, enabled `SESSION_SECRET` version, an approved test access method,
and rollback readiness.

Predefine observation duration and failure thresholds before any live request.
Controlled testing must cover startup, health, request-log privacy, proxy/IP
behavior, cookies, access and recovery redemption, intended HTTPS origin,
test-only Airtable fields, approved Gemini behavior, controlled GHL delivery,
monitoring, and failure visibility. Participant enrollment remains unauthorized.

## 9. Separately authorized fixed-revision traffic movement

Before each movement, bind authorization to the exact project, region, service,
candidate and baseline revisions/digests, complete current map, complete target
map, unchanged tag map or explicitly reviewed tag change, observation duration,
failure thresholds, and rollback target.

Use fixed revisions only. Percentages must explicitly name every serving fixed
revision and total 100 so gcloud cannot proportionally adjust an omitted target.
For each individually authorized movement, supply separately approved complete
current and target service-state JSON files, then execute:

```bash
compare_complete_map() {
  local purpose="$1" observed_file="$2" expected_file="$3"
  python3.12 scripts/validate_deployment_state.py traffic-map \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --purpose="$purpose" --evidence-root="$EVIDENCE_ROOT" \
    --observed-file="$observed_file" --expected-file="$expected_file"
}

(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'traffic movement gate failed: %s\n' "$1" >&2; exit 1; }
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  : "${AUTHORIZED_CURRENT_MAP_FILE:?approved current map is required}"
  : "${AUTHORIZED_TARGET_MAP_FILE:?approved target map is required}"
  compare_complete_map TRAFFIC "$AUTHORIZED_TARGET_MAP_FILE" \
    "$AUTHORIZED_TARGET_MAP_FILE"
  COMPLETE_FIXED_TARGET_MAP="$(python3.12 scripts/validate_deployment_state.py traffic-map \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --purpose=TRAFFIC --evidence-root="$EVIDENCE_ROOT" \
    --observed-file="$AUTHORIZED_TARGET_MAP_FILE" \
    --expected-file="$AUTHORIZED_TARGET_MAP_FILE" --output=command-map)" \
    || fail 'authorized command-map derivation'
  CURRENT_TAG_MAP="$(python3.12 scripts/validate_deployment_state.py traffic-map \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --purpose=TRAFFIC --evidence-root="$EVIDENCE_ROOT" \
    --observed-file="$AUTHORIZED_CURRENT_MAP_FILE" \
    --expected-file="$AUTHORIZED_CURRENT_MAP_FILE" --output=tag-map)" \
    || fail 'authorized current tag-map derivation'
  TARGET_TAG_MAP="$(python3.12 scripts/validate_deployment_state.py traffic-map \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --purpose=TRAFFIC --evidence-root="$EVIDENCE_ROOT" \
    --observed-file="$AUTHORIZED_TARGET_MAP_FILE" \
    --expected-file="$AUTHORIZED_TARGET_MAP_FILE" --output=tag-map)" \
    || fail 'authorized target tag-map derivation'
  [[ "$CURRENT_TAG_MAP" == "$TARGET_TAG_MAP" ]] || fail 'authorized tag-map change'
  [[ -n "$COMPLETE_FIXED_TARGET_MAP" ]] || fail 'empty target command map'
  OBSERVED_PRE="$EVIDENCE_ROOT/traffic-movement-pre.json"
  OBSERVED_POST="$EVIDENCE_ROOT/traffic-movement-post.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$OBSERVED_PRE" \
    --output-file="$OBSERVED_POST" >/dev/null \
    || fail 'unsafe or preexisting traffic evidence path'

  gcloud run services describe "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(status.latestReadyRevisionName,status.traffic)' \
    > "$OBSERVED_PRE"
  compare_complete_map TRAFFIC "$OBSERVED_PRE" "$AUTHORIZED_CURRENT_MAP_FILE"

  gcloud run services update-traffic "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --to-revisions="$COMPLETE_FIXED_TARGET_MAP" --quiet

  gcloud run services describe "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(status.latestReadyRevisionName,status.traffic)' \
    > "$OBSERVED_POST"
  compare_complete_map TRAFFIC "$OBSERVED_POST" "$AUTHORIZED_TARGET_MAP_FILE"
)
```

Do not use `LATEST`, `--to-latest`, or a partial percentage map. This standard
block requires the authorized current and target tag maps to be identical before
mutation; it does not authorize tag changes. Any current-map mismatch stops
before mutation. Any post-map mismatch or failed observation stops further
movement and requires a separately authorized rollback; do not automatically
retry.

## 10. Separately authorized rollback

Rollback targets the exact known-good fixed revision and immutable digest, never
whatever happens to be latest. Require:

- the exact expected current map before mutation;
- exact readiness and digest for the known-good revision;
- a complete fixed-revision rollback percentage map totaling 100;
- the complete recorded tag map;
- separate rollback authorization bound to those values.

After separate rollback authorization bound to the complete files and variables
below, execute exactly one rollback command and one post-query:

```bash
(
  set -euo pipefail
  set -o noclobber
  fail() { printf 'rollback gate failed: %s\n' "$1" >&2; exit 1; }
  : "${EVIDENCE_ROOT:?preapproved evidence directory is required}"
  : "${AUTHORIZED_ROLLBACK_CURRENT_MAP_FILE:?approved current map is required}"
  : "${AUTHORIZED_ROLLBACK_TARGET_MAP_FILE:?approved rollback map is required}"
  compare_complete_map ROLLBACK "$AUTHORIZED_ROLLBACK_TARGET_MAP_FILE" \
    "$AUTHORIZED_ROLLBACK_TARGET_MAP_FILE"
  COMPLETE_FIXED_ROLLBACK_MAP="$(python3.12 scripts/validate_deployment_state.py traffic-map \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --purpose=ROLLBACK --evidence-root="$EVIDENCE_ROOT" \
    --observed-file="$AUTHORIZED_ROLLBACK_TARGET_MAP_FILE" \
    --expected-file="$AUTHORIZED_ROLLBACK_TARGET_MAP_FILE" --output=command-map)" \
    || fail 'rollback command-map derivation'
  COMPLETE_ROLLBACK_TAG_MAP="$(python3.12 scripts/validate_deployment_state.py traffic-map \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --purpose=ROLLBACK --evidence-root="$EVIDENCE_ROOT" \
    --observed-file="$AUTHORIZED_ROLLBACK_TARGET_MAP_FILE" \
    --expected-file="$AUTHORIZED_ROLLBACK_TARGET_MAP_FILE" --output=tag-map)" \
    || fail 'rollback tag-map derivation'
  if [[ "$COMPLETE_ROLLBACK_TAG_MAP" == - ]]; then
    ROLLBACK_TAG_MODE=CLEAR
  else
    ROLLBACK_TAG_MODE=SET
  fi

  gcloud run revisions describe "$BASELINE_REVISION" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(metadata.name,status.conditions,status.imageDigest)' \
    | bind_scope revision \
    | python3.12 scripts/validate_deployment_state.py revision \
        --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
        --expected-revision="$BASELINE_REVISION" \
        --expected-image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}" \
        --expected-digest="$BASELINE_DIGEST"

  ROLLBACK_PRE="$EVIDENCE_ROOT/rollback-pre.json"
  ROLLBACK_POST="$EVIDENCE_ROOT/rollback-post.json"
  python3.12 scripts/validate_deployment_state.py evidence-path \
    --project="$PROJECT_ID" --region="$REGION" --service="$SERVICE" \
    --evidence-root="$EVIDENCE_ROOT" --output-file="$ROLLBACK_PRE" \
    --output-file="$ROLLBACK_POST" >/dev/null \
    || fail 'unsafe or preexisting rollback evidence path'
  gcloud run services describe "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(status.latestReadyRevisionName,status.traffic)' \
    > "$ROLLBACK_PRE"
  compare_complete_map ROLLBACK "$ROLLBACK_PRE" \
    "$AUTHORIZED_ROLLBACK_CURRENT_MAP_FILE"

  if [[ "$ROLLBACK_TAG_MODE" == SET ]]; then
    gcloud run services update-traffic "$SERVICE" \
      --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
      --set-tags="$COMPLETE_ROLLBACK_TAG_MAP" \
      --to-revisions="$COMPLETE_FIXED_ROLLBACK_MAP" --quiet
  else
    gcloud run services update-traffic "$SERVICE" \
      --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
      --clear-tags --to-revisions="$COMPLETE_FIXED_ROLLBACK_MAP" --quiet
  fi

  gcloud run services describe "$SERVICE" \
    --account="$ACCOUNT" --project="$PROJECT_ID" --region="$REGION" \
    --format='json(status.latestReadyRevisionName,status.traffic)' \
    > "$ROLLBACK_POST"
  compare_complete_map ROLLBACK "$ROLLBACK_POST" \
    "$AUTHORIZED_ROLLBACK_TARGET_MAP_FILE"
)
```

Installed help specifies that tag changes occur before the percentage change
when both are supplied. Any malformed input, precondition mismatch, command
failure, or final mismatch stops and reports failure without an automatic second
traffic command.

## 11. Remaining gates

Corrected local procedure tests do not prove Cloud permissions, exact current
configuration, build success, candidate behavior, or rollback behavior. Before
deployment or participant enrollment, separately authorize and verify:

- the corrected `SESSION_SECRET` reference query and enabled exact version;
- all required safe runtime metadata and authentication policy;
- exact candidate tag and revision nonexistence;
- build and Artifact Registry permissions;
- immutable candidate build evidence;
- candidate readiness, zero traffic, and configuration preservation;
- test-only Cloud Run, Airtable, Gemini, GHL, browser, proxy, and log behavior;
- monitoring thresholds and rollback rehearsal.

Deployment, traffic movement, rollback, live testing, and participant enrollment
remain unauthorized until their respective explicit approvals.
