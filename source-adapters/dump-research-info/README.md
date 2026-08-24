# `dump-research-info` source adapter

This site-owned adapter imports the legacy `data/con_site` class arrays from one exact, clean [`con/dump-research-info`](https://github.com/con/dump-research-info) Git checkout.
It is deliberately CON-specific.

`metadata_adapter.py` owns only the reviewed source behavior:

- read `XYZ*.json` class arrays from `data/con_site`;
- match an exact PID first, then one unique same-class PID or DOI identifier;
- retain the accepted canonical PID and path for matched records;
- mint only the existing class-specific PID and filename forms for unmatched source records; and
- retarget the reviewed relationship shapes through those matches.

`candidates.py` maps those source facts into the shared `CandidatePlan` contract.
It runs the pinned ownership-aware enrichment helpers through the engine's companion-aware wrappers.
Populated topical fields remain curated; source values are represented by stored qualified assertions.
If a topical field is missing and an equivalent unowned assertion already exists, the upstream convenience copy is proposed without new PAV.
Imported assertion objects retain no inline machine PAV; the mirrored annotation companion stores only `pav:importedBy` and `pav:importedFrom` selectors.

The claim digest covers the baseline-independent semantic mapping: its selected class, topical values, qualified assertions, imported objects, and any policy-created fields.
It excludes PAV, human baseline content, formatting, and unused input, so a material mapping change reopens review without tying a compatible result to an implementation version.

The caller must supply:

- the exact canonical-metadata base commit;
- the exact source commit;
- the source checkout; and
- a `SchemaView` constructed by the trusted host from the pinned schema in the released runtime; and
- a reviewed versioned Thing/PID for this adapter that already exists in the canonical record tree.

There is intentionally no built-in production adapter PID.
The source revision belongs in the proposal's Git/DataLad provenance, while `pav:importedFrom` uses the stable logical source-record URL.

The plan is ephemeral.
Proposal generation writes only the candidate record and annotation-companion paths through the shared canonical writer.
The adapter's optional compact cache is `policy/curation-decisions.yaml`; a missing file is the valid empty state.

Source absence never proposes deletion.
A deletion must be an explicit future source-policy change or a separate human curation action.
The adapter does not write to `dump-research-info`, retain inventories or transaction records, or choose a review disposition.

If a field or explicit attribute predicate disappears from a later source record, the adapter gives the pinned helper an empty update.
That removes only the obsolete qualified assertions and imported objects owned by this adapter; curated topical values and human- or differently owned assertions remain.

For local reproduction, the trusted host loads `candidates.py` and calls `build_candidate_plan(root, source_checkout, *, metadata_base, expected_source_commit, adapter_agent_pid, schema)`.
The host constructs `schema` from `runtime/schema/demo-research-information/unreleased.yaml` in the verified released runtime.
Source data is always read from the reviewed `data/con_site` directory; changing that mapping is adapter policy, not a per-run option.

Normal reviewers do not need a local checkout.
The trusted GitHub workflow performs source acquisition and candidate generation, opens the metadata PR, and binds review submission to the exact source, proposal, and head.
