# GitHub curation review prototype v1

Status: site-owned hosted-review prototype; no compatibility promise

This workflow puts source proposal, human review, reconciliation, and ordinary
pull-request approval in one draft GitHub pull request. A reviewer does not
need a local checkout. The existing command-line workflow remains available
for people who prefer it.

The automation records only explicit reviewer choices. It never chooses a
disposition, approves or merges its pull request, deploys a preview, writes to
an upstream source, or bypasses normal branch protection.

## Public review boundary

A proposal inventory contains complete baseline and proposed records. Review
comments and the durable decision ledger contain GitHub reviewer identity,
rationale, and evidence links. In a public repository these values remain
public in the pull request and Git history.

The Actions form therefore requires a transaction-specific acknowledgment
before it will create a pull request. The manifest records the initiating
GitHub actor, workflow-run URL, and timestamp. Comment processing re-reads that
immutable workflow run instead of trusting editable pull-request files.

## Open one review pull request

1. On the repository's **Actions** tab, choose **Curate source metadata** and
   select **Run workflow** on the default branch.
2. Choose `dump-research-info` or `zotero`, enter the exact evaluation date,
   and provide the adapter's immutable source coordinate. For
   `dump-research-info`, this is a full 40-character commit ID.
3. Read and select the public-review-data acknowledgment, then start the run.

The workflow reproduces the proposal from the selected source, retains a
DataLad sidecar, and opens one draft branch named
`automation/curation/<adapter>-<run-id>`. A zero-candidate proposal completes
with an Actions summary and does not open an empty pull request.

The pull request links a generated review document. Candidate aliases such as
`DRI-001` are only short review handles; the manifest binds every alias to the
full candidate and claim-revision identities. Each candidate card includes:

- source identity and proposed canonical path;
- blockers that prevent acceptance;
- the complete baseline and proposed values and their semantic diff.

Separate copyable YAML forms group the candidate decision items into batches
of at most 20.

Proposal, manifest, review form, source pins, and initial DataLad evidence are
immutable review inputs. The workflow rejects unexpected paths, modes,
symlinks, executable changes, forged coordinates, or a proposal that cannot be
reproduced byte-for-byte from trusted default-branch code and the recorded
source.

## Submit explicit decisions

Copy one of the generated batches into a new pull-request comment. Each comment
must have exactly this shape:

````text
/curation submit
```yaml
inventory_id: curation-inventory-v1:REPLACE_WITH_EXACT_ID
decisions:
  - candidate: DRI-001
    expected_decision: null
    disposition: reject
    rationale: Explain the reviewed choice.
    evidence:
      - https://example.org/reviewed-source
    details: {}
```
````

The reviewer replaces `REPLACE_ME` in generated forms with one of the supported
dispositions and supplies the required rationale, evidence, and conditional
details. Generated forms contain at most 20 decisions. GitHub derives the
reviewer identity, decision date, and comment permalink; values in the comment
cannot spoof them.

Only a collaborator whose GitHub permission check reports `write` or `admin`
can submit a batch. The batch is atomic: any malformed, unknown, stale,
duplicate, blocked, or incomplete item rejects the whole comment. Successful
comments add immutable decision events on the same pull-request branch and post
an attributed progress reply with current decision IDs.

Before finalization, correct a choice by posting a new decision item with
`expected_decision` set to the current event ID from the bot's progress reply.
The new event supersedes the old event without deleting review history. Comment
edits and deletions never rewrite an accepted event.

## Finalize once

When every candidate has one current explicit outcome, post a comment whose
entire body is:

```text
/curation finalize
```

Finalization is terminal in prototype v1. The workflow rechecks collaborator
authority, pull-request provenance, the immutable proposal run, the complete
decision transaction, and a trusted byte-for-byte proposal reproduction. It
then reconciles the exact reviewed transaction once, validates the staged
metadata with the locked runtime, retains the report and second DataLad
sidecar, replays the result from the trusted base, and pushes the canonical
change to the same draft pull request with an exact head-SHA lease.

The bot explicitly dispatches read-only validation for the final head. Any bot
push dismisses stale approval under the repository's branch rules, so a human
must review and approve the reconciled head. The bot never marks the pull
request ready, approves it, merges it, or deploys it.

To change a decision after finalization, close the unmerged pull request and
start a new transaction. Prototype v1 intentionally forbids correction after
canonical reconciliation because a non-accepting decision does not itself undo
an earlier accepted record.

## Trust and failure behavior

The privileged comment workflow is loaded from the default branch. It treats
the pull-request checkout as strict data and does not run changed pull-request
code with a write token. Proposal regeneration and reconciliation use trusted
default-branch implementation files and the pinned runtime. Branch updates are
serialized per pull request and use compare-and-swap pushes, so a force push or
concurrent edit fails without overwriting either version.

Only `issue_comment.created` events cause processing. Later edits or deletion
of a reviewer's submission do not trigger or rewrite an accepted decision; the
bot's separately attested ledger remains authoritative. Editing or removing a
bot receipt makes later commands fail closed. Current GitHub queueing
serializes the per-pull-request command runs without discarding pending review
submissions. Failed commands leave an attributed Actions result and do not
infer or partially apply a decision.

For the lower-level formats, dispositions, recovery rules, and optional local
commands, see `CURATION-PROTOTYPE-V1.md` in this directory.
