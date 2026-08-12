# Provenance scope

The seven YAML ledgers in this directory are retained byte-for-byte from the
accepted Milestone 3 site commit
`26907c487efaa2c31bba9d02398aa201ab6f774b`.

In particular, `selection.yaml` records the historical Milestone 1 and 2
legacy-site selection process. Its `reviewed-subset` status and six-record
vertical-slice counts are evidence about that earlier migration step. They are
not a selection policy for this downstream repository.

This consumer includes the complete accepted Milestone 3 profile without a
record-selection filter: 186 canonical records, 13 reference records, all ten
editorial sources, all 71 declared assets, and the complete committed
projection. `generated/manifests/full-fidelity.json` is the executable
full-snapshot contract.

`active-snapshot.json` is the concise active provenance pointer used by this
consumer. It declares `selection_policy.mode: all`, a null filter, and the full
site-bundle scope. No live importer or renderer reads `selection.yaml`.
