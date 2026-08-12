# CON Zotero snapshots

`snapshot.json` is generated from the public Zotero API by:

```sh
pixi run zotero-fetch
```

It records the collection definitions, all top-level items, retrieval time, API
URLs, requested and returned API versions, one consistent Zotero library
version, record counts, and a normalized content digest needed to interpret a
transform. Acquisition retries the complete snapshot if collection and item
pages do not report the same library version. It honors Zotero `Backoff` and
`Retry-After` requests and never requires an API key for this public library.

The snapshot is source evidence, not a validated research-information file,
and must never be loaded by `dump-research-info` directly. Candidate generation
verifies its provenance, ordering, unique keys, counts, and digest before
transforming it.

`site-migration.yaml` is the reviewed translation boundary for the static CON
profile. It lists permitted and deliberately omitted relationship targets plus
the one preserved publication PID override. Every behavioral policy entry must
match the current source records; stale entries and unknown policy fields stop
the export. Likewise, a new or missing Zotero creator role stops ingestion
until its MARC relationship has been reviewed. `pixi run
zotero-site-candidates` first verifies that promoted JSON is current, then
renders isolated candidate YAML and a provenance report under ignored
`build/` state. The exporter rejects destinations outside that repository build
area, path overlaps, and symlinks, and stages the complete result before
replacing an older candidate artifact. It never writes the website repository.
