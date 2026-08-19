# GitHub curation review prototype v1

Status: site-owned hosted-review prototype; no compatibility promise

The hosted workflow keeps proposal, human review, and reconciliation in one
draft GitHub pull request. A reviewer does not need a local checkout. People
who prefer local tools may still use them, but local work is not required by
the hosted path.

The automation records explicit reviewer choices. It never chooses a
disposition, marks the pull request ready, approves or merges it, deploys it,
or writes to an upstream source.

## Start one review pull request

On the repository's **Actions** tab, run **Curate source metadata** from the
default branch and supply the source adapter inputs. The workflow resolves the
selected source revision and opens one draft pull request. If there is nothing
new to review, the run ends without opening an empty pull request.

The metadata diff, reviewer identity, and resulting decision cache are public
in this public repository. The Actions form requires an acknowledgment of that
boundary before starting the proposal.

The initial pull-request commit contains the proposed canonical metadata. Its
**Files changed** tab is the primary review surface: it shows the ordinary Git
diff that would be accepted for each record.

DataLad is used only to create this initial metadata-producing commit. Its run
record is kept inline in the commit rather than written as a sidecar file.
Submission and reconciliation use ordinary Git commits.

The pull-request body supplies the corresponding decision controls. Each
record is headed by a friendly existing label and canonical PID, with the
source-native identifier when useful. Internal digests are not presented as
record names.

This is a native GitHub pull-request interface built from Markdown task lists;
it is not a GitHub Issue Form.

## Review and submit

For every record, review its metadata diff and use the task-list controls in
the pull-request body to check exactly one of:

- **Accept** -- retain the proposed metadata;
- **Reject** -- do not add or replace the metadata; or
- **Defer** -- make no metadata change in this review and return the record in
  the next proposal.

A record with an acceptance blocker cannot be accepted. Rejecting or deferring
it removes the proposed metadata change during reconciliation.

After every record has exactly one choice, create a pull-request comment whose
entire body is:

```text
/curation submit
```

The comment is only the submit action. Decisions come from the task-list state
in the pull-request body; reviewers do not copy YAML or opaque identifiers into
comments. A submission fails without changing the branch if choices are
missing, conflicting, stale, or otherwise invalid.

Only a collaborator with suitable repository permission can submit. The bot
uses trusted default-branch workflow code, regenerates the proposal from the
pinned source revision, and verifies that the proposed metadata matches before
applying the choices.

## Result of submission

The bot adds one ordinary Git commit to the same draft pull request. That
commit:

- keeps accepted metadata;
- restores or removes rejected and deferred metadata;
- updates the compact current decision cache; and
- starts normal metadata validation for the reconciled head.

The cache is needed in particular for rejected and deferred records, whose
human decisions leave no canonical metadata diff. It stores the current
disposition under a stable, human-meaningful record identity and one internal
claim digest for cache invalidation. An unchanged rejection stays cached;
deferral deliberately returns for the next review. The source revision,
reviewer, decision time, and pull-request URL are stored once for the review
rather than repeated for every record. Git history preserves earlier cache
states and is the decision history.

## Bounded upstream provenance alignment

The reviewed commit follows the German upstream's author/committer distinction
without introducing another audit store. The authenticated reviewer is the Git
author and `github-actions[bot]` is the committer. Standard commit trailers
record the adapter, exact source coordinate, review URL, and review time.
Ordinary Git history records each YAML version, while the PID-keyed cache links
every human disposition to its reviewed commit. Their histories together form
the append-only per-record audit. An accepted record is not rewritten merely
to make its path appear in the later review commit. No generated audit report
is tracked.

Accepted records carry semantic source provenance from the adapter. Every
imported record has expanded PAV `importedFrom` and `importedBy` annotations.
The dump adapter also places the same annotations on imported structured
assertions such as identifiers, attributions, attribute specifications, and
generations. Direct scalar properties remain covered by the record-level
annotation. The adapter must never label a pre-existing human-maintained or
site-policy assertion as an upstream import.

Assertion-level PAV is added to one adapter first. Zotero remains at
record-level provenance until its source assertions can be distinguished from
site-policy additions and pass the same locked schema round-trip tests. A
shared abstraction is considered only after both adapters demonstrate the same
semantics.

This use of PAV, and related PROV ontology patterns where needed, is the point
of alignment with upstream provenance work. It does not imply copying an
upstream Git or DataLad management strategy.

There is no tracked proposal inventory, generated review document, exhaustive
manifest, reconciliation report, custom attestation chain, DataLad sidecar, or
separate finalize command. The pull request, its metadata diff, its compact
decision cache, the adapter's semantic provenance, and ordinary Git history
carry the review evidence.

The bounded audit and assertion changes do not alter that storage boundary.
DataLad remains limited to the initial metadata-producing commit, and
submission remains one ordinary Git commit.

## Checks and review state

The initial proposal intentionally contains the metadata under review. Normal
checks can therefore be red before the reviewer rejects or defers blocked
proposals. The meaningful validation result is the one run against the
reconciled head after `/curation submit`.

The pull request remains a draft after the bot's commit. Normal branch
protection and human pull-request review still apply. The bot never approves,
merges, or deploys the result.
