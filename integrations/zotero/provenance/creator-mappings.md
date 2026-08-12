# Zotero creator mappings

The Zotero transformer resolves creator names against modeled `XYZPerson` and
`XYZOrganization` records using normalized exact matching. It does not perform
fuzzy matching.

Reviewed exceptions are stored in
`inputs/zotero_centerforopenneuroscience/creator-mappings.yaml`. Each mapping
groups aliases under one existing PID and records a rationale plus source
evidence. Mappings cannot create people, and the transform fails when a target
PID is absent from the supplied people or organization indexes.

This separation keeps three cases visible:

- Exact modeled names resolve automatically.
- Reviewed name variants resolve through the mapping file.
- External authors and ambiguous names remain in `unresolved_creators` for
  later modeling or review.

To rebuild candidates and their reconciliation report, run:

```bash
pixi run zotero-candidates
```

The report records the mapping file, alias count, target count, per-alias use,
and the unresolved creator queue. Publication source data should continue to be
maintained in Zotero; the mapping file only reconciles identities already
represented in this repository.
