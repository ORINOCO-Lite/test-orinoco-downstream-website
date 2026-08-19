# Static curation prototype v1

Status: site-owned Milestone 5 prototype; no compatibility promise

This prototype separates a deterministic source proposal, an explicit human
decision, and canonical reconciliation.
It does not write to Zotero, approve or merge a pull request, infer a
disposition, or choose who is authorized to review.
The engine, template, and production site do not expose this as a supported
interface.

Candidate inventories contain complete baseline and proposed records.
Decision events contain reviewer identity, rationale, and evidence.
Review the privacy and retention consequences before committing either kind of
file to a public repository.
The Milestone 5 implementation uses real inventories only as local acceptance
evidence until those choices receive human review.

## Fixed boundaries

- Canonical Things remain only below `metadata/records/`.
- Inventories and reports use `source-adapters/<adapter>/transactions/*.yaml`.
- Durable decision events use `source-adapters/<adapter>/policy/*.yaml`.
- Provider output and inventory publication scratch remain ignored below
  `build/curation/<adapter>/`.
- Every command uses literal source versions, dates, paths, and policy-question
  identifiers.
  Run IDs never identify a candidate or decision.
- Reconciliation requires the exact locked Orinoco Lite 0.1.12 engine and
  runtime and validates the entire staged metadata tree before activation.

The command facade is:

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py --help
```

## 1. Propose from an exact source

Use an explicit evaluation date even when no deferral is date-based.
Repeat `--resolved-policy-question` only for questions a human has actually
resolved.
Omit `--decisions` only when no durable ledger exists yet.

Frozen Zotero example:

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py propose \
  --adapter zotero \
  --inventory \
    source-adapters/zotero/transactions/zotero-v451-2026-08-18.yaml \
  --decisions source-adapters/zotero/policy/curation-decisions.yaml \
  --provider-output build/curation/zotero/v451-2026-08-18 \
  --as-of 2026-08-18 \
  --expected-library-version 451
```

Frozen `dump-research-info` example:

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py propose \
  --adapter dump-research-info \
  --inventory \
    source-adapters/dump-research-info/transactions/dump-062da59.yaml \
  --decisions \
    source-adapters/dump-research-info/policy/curation-decisions.yaml \
  --provider-output build/curation/dump-research-info/062da59 \
  --as-of 2026-08-18 \
  --source-path ../dump-research-info \
  --expected-source-commit \
    062da59cb5a00ca128b3df895426a54088bfc625
```

The dump provider resolves the literal path from the downstream root and rejects
a dirty checkout or the wrong revision.
Relocating an otherwise exact checkout changes execution evidence, not candidate
identity or material state.
Both providers reject unsafe paths, never change the decision ledger, and
publish an inventory only after its ignored scratch file is complete and
fsynced.

For retained execution evidence, wrap the resolved proposal command in the
project-local DataLad environment.
Declare the exact source, policy, canonical baseline, implementation, and
inventory output; do not hide operative values in environment variables.

```console
pixi run datalad run --explicit --sidecar yes \
  -m "propose frozen Zotero v451 curation inventory" \
  -i pixi.toml \
  -i pixi.lock \
  -i orinoco.yaml \
  -i orinoco.lock \
  -i metadata/records \
  -i source-adapters/zotero/source/snapshot.json \
  -i source-adapters/zotero/source/candidates/XYZPublication.json \
  -i source-adapters/zotero/policy \
  -i source-adapters/zotero/curation_prototype_v1.py \
  -i source-adapters/zotero/metadata_adapter.py \
  -i source-adapters/zotero/tools/zotero_ingest.py \
  -i source-adapters/zotero/tools/zotero_site_export.py \
  -i source-adapters/metadata/tools/curation_prototype_v1.py \
  -i source-adapters/metadata/tools/curation_cli_prototype_v1.py \
  -o source-adapters/zotero/transactions/zotero-v451-2026-08-18.yaml \
  "pixi run python \
    source-adapters/metadata/tools/curation_cli_prototype_v1.py propose \
    --adapter zotero \
    --inventory \
      source-adapters/zotero/transactions/zotero-v451-2026-08-18.yaml \
    --decisions source-adapters/zotero/policy/curation-decisions.yaml \
    --provider-output build/curation/zotero/v451-2026-08-18 \
    --as-of 2026-08-18 \
    --expected-library-version 451"
```

## 2. Record explicit decisions

Inspect every inventory candidate and its blockers.
Use `render-decision` to derive the claim-revision and decision-event
identifiers from one reviewed candidate.
It prints YAML to standard output and never edits the ledger.

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py render-decision \
  --adapter zotero \
  --inventory \
    source-adapters/zotero/transactions/zotero-v451-2026-08-18.yaml \
  --candidate-id curation-candidate-v1:<64-hex-digest> \
  --disposition reject \
  --reviewer <reviewed-identity> \
  --decided-on 2026-08-18 \
  --rationale "<reviewed rationale>" \
  --evidence "<reviewed evidence reference>"
```

For a later event on the same claim, pass the current event with
`--supersedes-decision-id`.
Conditional dispositions require exactly one YAML detail:

- link: `{target_record_id: <canonical-pid>}`;
- defer: `{return_when: {kind: material-change}}`,
  `{return_when: {kind: relevant-policy-change}}`,
  `{return_when: {kind: on-or-after, date: "YYYY-MM-DD"}}`, or
  `{return_when: {kind: policy-question-resolved, question: <id>}}`;
- permanent exclusion: `{scope: {...}}`, including explicit source/claim axes,
  `material_changes: true`, and a boolean `relevant_policy_changes`; and
- supersede: `{replacement_candidate_id: <candidate-id>}`.

Append the reviewed event without changing prior events.
Add one transaction entry that binds the exact inventory ID to exactly one
active decision-event ID for every candidate.
Every retained event must remain anchored by a retained transaction.
The parser rejects missing, extra, stale, branching, unbound, or contradictory
events.

## 3. Reconcile and validate

Verify the immutable runtime first:

```console
pixi run verify-runtime
```

Then reconcile the reviewed inventory and ledger.
A real review branch should record this command with
`datalad run --explicit --sidecar yes`, declaring the inventory and ledger as
inputs and `metadata/records` plus the append-only report as outputs.

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py reconcile \
  --adapter zotero \
  --inventory \
    source-adapters/zotero/transactions/zotero-v451-2026-08-18.yaml \
  --decisions source-adapters/zotero/policy/curation-decisions.yaml \
  --report \
    source-adapters/zotero/transactions/zotero-v451-reconciled.yaml
```

Reconciliation requires complete current decisions, exact baselines, unique link
targets and PIDs, and a schema-valid staged tree.
It holds an exclusive canonical lock, compares the tree before and immediately
before activation, installs the whole tree by rename, verifies the installed
digest before retiring the rollback authority, and finalizes a deterministic
report while the lock is still held.
An identical accepted rerun is a no-op; a materially or relevant-policy changed
claim returns to review.

Review the final pull-request head after reconciliation.
Automation does not approve or merge it.
Keep the inventory, ledger, report, and DataLad sidecar in the final tracked
tree if their retention has been accepted; this makes the execution and review
evidence independent of whether the repository uses a squash or rebase merge.

The checked-in acceptance test uses a synthetic all-rejected transaction to
prove that all three curation artifacts and the DataLad sidecar remain parseable
after a local squash and after the original run commit is pruned.
It does not stand in for the required human-reviewed pull request or a hosted
default-branch transition.

## Explicit recovery

Never remove transaction artifacts by hand before determining which authority
is intact.
The CLI reports the exact next command when possible.

If a dead process left the canonical lock, first verify that its recorded PID is
gone, then use a new append-only report path:

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py recover-lock \
  --adapter zotero \
  --report source-adapters/zotero/transactions/recovered-lock.yaml
```

If a stage or rollback backup remains, recover the canonical tree next:

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py recover \
  --adapter zotero \
  --report source-adapters/zotero/transactions/recovered-tree.yaml
```

Finally, if a report path contains a reservation marker or has a token-bound
temporary sibling, use the token shown in the diagnostic or filename:

```console
pixi run python \
  source-adapters/metadata/tools/curation_cli_prototype_v1.py \
  recover-report-reservation \
  --adapter zotero \
  --report source-adapters/zotero/transactions/zotero-v451-reconciled.yaml \
  --token <32-lower-case-hex-token>
```

Recovery holds the same canonical lock and compares exact before/after digests
and artifact sets.
It finalizes a prepared report only for the matching installed state, discards
only a proven unactivated plan, and otherwise fails closed for manual review.
Automatic stale-owner checks are supported on the milestone's macOS and Linux
platforms; other platforms require manual verification.
