# Controlled deployment provenance and rollback runbook

This is a future operator procedure. It has not been executed or rehearsed,
the container has not been built or published under this procedure, and the
merged application has not been deployed. Every build, Cloud, integration,
traffic, and participant-enrollment step requires separate explicit
authorization from the project owner.

The commands below are command structures for the existing Cloud Build,
Artifact Registry, and Cloud Run architecture. They have not been live-tested
by this repository-only review. Do not run them until the named infrastructure
values and the required authorization have been confirmed.

## 1. Define and validate release placeholders

Use Bash. Replace every `REPLACE_...` value with an explicitly approved value.
An unresolved placeholder is intentionally rejected by `require_value`. Run
the applicable validation immediately before every operational command block.

```bash
PROJECT_ID='REPLACE_WITH_PROJECT_ID'
REGION='REPLACE_WITH_REGION'
SERVICE='REPLACE_WITH_SERVICE'
AR_REPOSITORY='REPLACE_WITH_ARTIFACT_REGISTRY_REPOSITORY'
IMAGE_NAME='REPLACE_WITH_IMAGE_NAME'
SOURCE_SHA='REPLACE_WITH_FULL_40_CHARACTER_SOURCE_SHA'
CANDIDATE_REVISION='REPLACE_WITH_FULL_CANDIDATE_REVISION_NAME'
PREVIOUS_REVISION='REPLACE_AFTER_TRAFFIC_PREFLIGHT'
PREVIOUS_IMAGE_DIGEST='REPLACE_AFTER_PREVIOUS_REVISION_INSPECTION'
CANDIDATE_IMAGE_DIGEST='REPLACE_AFTER_AUTHORIZED_BUILD'

require_value() {
  local name value
  name="$1"
  value="${!name-}"
  if [[ -z "$value" || "$value" == REPLACE_* ]]; then
    printf 'Required release value is unresolved: %s\n' "$name" >&2
    return 1
  fi
}
```

These variables are identifiers and digests only. Never assign credential or
participant values to them. The release record must not contain plaintext
environment values, secret contents, bearer tokens, or participant data.

## 2. Establish the exact clean source

Validate and record the approved commit and tree before any Cloud operation.
Do not build from an unidentified working tree.

```bash
require_value SOURCE_SHA
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
test -z "$(git status --porcelain=v1 --untracked-files=all)"
test "$(git rev-parse HEAD)" = "$SOURCE_SHA"
test "$(git rev-parse --verify "${SOURCE_SHA}^{commit}")" = "$SOURCE_SHA"
git show -s --format='%H %T %s' "$SOURCE_SHA"
```

Stop if any check fails. Record the full source commit SHA, tree SHA, subject,
and the empty status result. The hash-locked installation, `pip check`, complete
mocked suite, and repository validation evidence must correspond to this same
source commit.

## 3. Preserve the serving rollback target

With separate read-only Cloud authorization, inspect only the service status
fields needed to identify current traffic. This filtered query does not request
the service specification or environment configuration.

```bash
require_value PROJECT_ID
require_value REGION
require_value SERVICE
gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'
```

Record the complete traffic assignment from that output. Set
`PREVIOUS_REVISION` to the explicitly approved revision that will receive 100%
traffic on rollback. If multiple revisions are serving, stop until the owner
approves the exact rollback target.

Read only that revision's resolved immutable image identity:

```bash
require_value PROJECT_ID
require_value REGION
require_value PREVIOUS_REVISION
PREVIOUS_IMAGE_ID="$(gcloud run revisions describe "$PREVIOUS_REVISION" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='value(status.imageDigest)')"
case "$PREVIOUS_IMAGE_ID" in
  sha256:*) PREVIOUS_IMAGE_DIGEST="$PREVIOUS_IMAGE_ID" ;;
  *@sha256:*) PREVIOUS_IMAGE_DIGEST="${PREVIOUS_IMAGE_ID##*@}" ;;
  *) printf 'Previous image identity is not immutable\n' >&2; false ;;
esac
[[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
printf 'previous_revision=%s\nprevious_image_digest=%s\n' \
  "$PREVIOUS_REVISION" "$PREVIOUS_IMAGE_DIGEST"
```

Stop before building unless the release record contains the previous revision,
its exact `sha256:` digest, and the original traffic map.

## 4. Secret-reference gate

Before candidate deployment, a separately authorized operator must confirm that
all required runtime settings use the approved Secret Manager references and
that `SESSION_SECRET` is present. Record secret names or resource references
only. Do not record or display environment values or secret payloads.

Cloud Run field shapes can vary by API surface and configuration. This runbook
therefore does not invent a secret-inspection command. Use only a separately
approved and verified field-filtered query that exposes reference metadata and
names without returning plaintext values. Secret Manager setup and this safe
reference verification remain deployment blockers until completed.

## 5. Build and obtain the immutable candidate digest

After separate build authorization, build the Dockerfile from the already
verified clean checkout. The Artifact Registry repository must already exist in
the approved region. The source SHA tag associates the build with the commit,
but the tag is not used as deployment identity.

```bash
require_value PROJECT_ID
require_value REGION
require_value AR_REPOSITORY
require_value IMAGE_NAME
require_value SOURCE_SHA
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
test -z "$(git status --porcelain=v1 --untracked-files=all)"
test "$(git rev-parse HEAD)" = "$SOURCE_SHA"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}"
gcloud builds submit . \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --tag="${IMAGE_URI}:${SOURCE_SHA}"
```

Record the build identifier, command, status, source commit, and tree. After the
authorized build reports success, resolve the tag through Artifact Registry and
record the immutable digest:

```bash
require_value PROJECT_ID
require_value SOURCE_SHA
require_value IMAGE_URI
CANDIDATE_IMAGE_DIGEST="$(gcloud artifacts docker images describe \
  "${IMAGE_URI}:${SOURCE_SHA}" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.digest)')"
[[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
CANDIDATE_IMAGE_REF="${IMAGE_URI}@${CANDIDATE_IMAGE_DIGEST}"
printf 'source_sha=%s\ncandidate_image=%s\n' \
  "$SOURCE_SHA" "$CANDIDATE_IMAGE_REF"
```

Do not proceed if the digest is empty or malformed. The release record must map
the source commit and tree to the build identifier and exact immutable image
reference. A mutable tag alone is never sufficient provenance.

## 6. Create a zero-traffic candidate

After separate deployment authorization and completion of the secret-reference
gate, validate the full revision name and deploy the immutable image reference.
The revision suffix is derived from the approved full candidate revision name.

```bash
require_value PROJECT_ID
require_value REGION
require_value SERVICE
require_value AR_REPOSITORY
require_value IMAGE_NAME
require_value CANDIDATE_REVISION
require_value CANDIDATE_IMAGE_DIGEST
[[ "$CANDIDATE_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$CANDIDATE_REVISION" == "${SERVICE}-"* ]]
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/${IMAGE_NAME}"
CANDIDATE_IMAGE_REF="${IMAGE_URI}@${CANDIDATE_IMAGE_DIGEST}"
CANDIDATE_REVISION_SUFFIX="${CANDIDATE_REVISION#${SERVICE}-}"
test "${SERVICE}-${CANDIDATE_REVISION_SUFFIX}" = "$CANDIDATE_REVISION"
gcloud run deploy "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --image="$CANDIDATE_IMAGE_REF" \
  --no-traffic \
  --revision-suffix="$CANDIDATE_REVISION_SUFFIX"
```

Do not add plaintext settings to this command. The immutable image path already
contains the explicit region, project, Artifact Registry repository, image name,
and digest. `--no-traffic` is mandatory.

## 7. Inspect the candidate without exposing configuration values

Use only the following filtered status and identity fields. Do not dump a full
service or revision resource.

```bash
require_value PROJECT_ID
require_value REGION
require_value SERVICE
require_value CANDIDATE_REVISION
gcloud run revisions describe "$CANDIDATE_REVISION" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='yaml(metadata.name,status.conditions.type,status.conditions.status,status.conditions.reason,status.imageDigest)'
gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'
```

Confirm and record all of the following before any live request:

- the candidate revision has a Ready condition;
- its `status.imageDigest` ends in the recorded `CANDIDATE_IMAGE_DIGEST`;
- it is absent from the traffic map or has zero percent traffic;
- the previously serving traffic assignment is unchanged;
- the approved secret-reference names and metadata passed the separate safe
  field-filtered inspection described in section 4.

Readiness alone does not authorize smoke testing or traffic movement.

## 8. Separately authorized candidate verification

Smoke testing and integration testing require separate authorization and
test-only contacts and records. At minimum, verify startup and health behavior,
request-log privacy, browser redemption and recovery origin behavior, test-only
Airtable field compatibility, approved Gemini behavior, controlled GHL delivery,
monitoring, and rollback-target visibility.

Participant enrollment remains unauthorized until every enrollment gate is met.

## 9. Gradual traffic movement

Traffic movement requires separate explicit authorization after the candidate
evidence is reviewed. Choose integer percentages whose sum is exactly 100.

```bash
APPROVED_PERCENT='REPLACE_WITH_APPROVED_CANDIDATE_PERCENT'
REMAINDER_PERCENT='REPLACE_WITH_PREVIOUS_REVISION_PERCENT'
require_value PROJECT_ID
require_value REGION
require_value SERVICE
require_value CANDIDATE_REVISION
require_value PREVIOUS_REVISION
require_value APPROVED_PERCENT
require_value REMAINDER_PERCENT
[[ "$APPROVED_PERCENT" =~ ^[0-9]+$ ]]
[[ "$REMAINDER_PERCENT" =~ ^[0-9]+$ ]]
(( APPROVED_PERCENT > 0 && APPROVED_PERCENT < 100 ))
(( APPROVED_PERCENT + REMAINDER_PERCENT == 100 ))
gcloud run services update-traffic "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --to-revisions="${CANDIDATE_REVISION}=${APPROVED_PERCENT},${PREVIOUS_REVISION}=${REMAINDER_PERCENT}"
gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'
```

Record each approved change and observation result. Do not move 100% of traffic
until the approved criteria and observation duration have passed.

## 10. Rollback

Rollback restores the exact recorded previous revision to 100% traffic without
rebuilding or redeploying an image.

```bash
require_value PROJECT_ID
require_value REGION
require_value SERVICE
require_value PREVIOUS_REVISION
require_value PREVIOUS_IMAGE_DIGEST
[[ "$PREVIOUS_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
gcloud run services update-traffic "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --to-revisions="${PREVIOUS_REVISION}=100"
gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'
gcloud run revisions describe "$PREVIOUS_REVISION" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='yaml(metadata.name,status.conditions.type,status.conditions.status,status.conditions.reason,status.imageDigest)'
```

Verify the traffic map, Ready condition, and resolved image digest against the
release record. Record the reason, time, operator, observed impact, and follow-up
decision. Preserve the candidate revision and image for investigation unless a
separately authorized retention or security response requires otherwise.
