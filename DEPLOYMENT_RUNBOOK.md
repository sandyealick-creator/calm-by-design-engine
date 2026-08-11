# Controlled deployment provenance and rollback runbook

This is a future operator procedure. It has not been executed or rehearsed,
and the merged application has not been deployed. Every Cloud or integration
step requires separate explicit authorization from the project owner.

## 1. Release evidence and preflight

Begin from a clean, reviewed Git commit. Do not deploy from an unidentified
working tree.

```sh
git status --porcelain=v1 --untracked-files=all
git rev-parse HEAD
git show -s --format='%H %T %s' HEAD
```

Record the full source commit SHA and tree SHA in the release record. Confirm
that the status output is empty and that the commit is the approved release
candidate. Run the hash-locked installation, `pip check`, the complete
socket-blocked mocked suite, and the repository validation checks before any
Cloud operation.

## 2. Preserve the serving rollback target

With separate read-only Cloud authorization, record the current traffic map,
the previously serving revision, and that revision's immutable image digest.
Do not infer these values from `latest` or from a mutable image tag.

```sh
gcloud run services describe cbd-assess \
  --project eng-drake-502618-h6 \
  --region us-east1 \
  --format=json

gcloud run revisions describe PREVIOUS_REVISION \
  --project eng-drake-502618-h6 \
  --region us-east1 \
  --format='value(spec.containers[0].image)'
```

The release record must contain `PREVIOUS_REVISION` and its `sha256:` image
digest before a candidate revision is created.

## 3. Build and record provenance

After explicit build authorization, build only from the approved clean source
commit. Tagging conventions are not provenance, so record the source SHA next
to the resulting immutable image digest returned by the authorized build.
Verify that the digest exists in Artifact Registry before deployment.

Required release-record fields:

- source commit SHA and tree SHA
- build identifier and completion status
- immutable candidate image digest
- build tool and command
- validation results produced from the same source commit

## 4. Deploy a zero-traffic candidate

After separate deployment authorization, deploy the immutable candidate image,
not a mutable tag. The new revision must receive zero traffic and the serving
revision must remain unchanged.

```sh
gcloud run deploy cbd-assess \
  --project eng-drake-502618-h6 \
  --region us-east1 \
  --image CANDIDATE_IMAGE_AT_SHA256_DIGEST \
  --no-traffic \
  --revision-suffix APPROVED_SUFFIX
```

Record the candidate revision name and re-read its image digest. Verify that
the candidate is Ready, receives zero percent traffic, and uses the approved
configuration names and secret references. Do not display environment values
or secret payloads. Do not move traffic merely because the revision is Ready.

## 5. Verification before traffic movement

Under a separate test authorization, use test-only contact information and
records. At minimum, verify:

- the candidate revision still maps to the recorded source and image digest
- startup and application health behavior
- required environment names and secret references
- request logging does not expose bearer tokens or participant content
- browser redemption and recovery origin behavior
- test-only Airtable field compatibility
- Gemini fallback and approved live-AI checks
- GHL workflow compatibility and controlled delivery
- monitoring and rollback target visibility

Participant enrollment remains unauthorized until its separate gate is met.

## 6. Controlled traffic movement

Traffic movement requires explicit authorization after the candidate evidence
is reviewed. Start with the approved small percentage while keeping the prior
revision available. Record every traffic change and its verification result.

```sh
gcloud run services update-traffic cbd-assess \
  --project eng-drake-502618-h6 \
  --region us-east1 \
  --to-revisions CANDIDATE_REVISION=APPROVED_PERCENT,PREVIOUS_REVISION=REMAINDER
```

Do not move 100 percent of traffic until the approved observation criteria and
duration have passed.

## 7. Rollback

Rollback means restoring the recorded prior revision to 100 percent traffic.
It does not require rebuilding or redeploying an image.

```sh
gcloud run services update-traffic cbd-assess \
  --project eng-drake-502618-h6 \
  --region us-east1 \
  --to-revisions PREVIOUS_REVISION=100
```

After rollback, verify the traffic map, Ready state, and immutable image digest
against the release record. Record the reason, time, operator, observed impact,
and follow-up decision. Do not delete the candidate revision or its image as
part of the immediate rollback.
