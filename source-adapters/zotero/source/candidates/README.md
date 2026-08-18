# Center for Open Neuroscience Zotero library

## Source

- Library: https://www.zotero.org/groups/6197458/centerforopenneuroscience/library
- Zotero group ID: `6197458`
- API root: https://api.zotero.org/groups/6197458
- Access: public, read-only ingestion

This directory contains validated JSON arrays generated from the CON Zotero
group. It is refreshed only through the snapshot, candidate, review, and
promotion boundaries described below.

## Inclusion policy

Include items assigned to these collections:

- `CON Articles`
- `CON Datasets`
- `CON Zenodo/OSF DOIs`
- `CON Software`

The importer also accepts the historical forms without the `CON` prefix so
that a collection-label migration does not silently reclassify records.

Exclude `External`. Treat unfiled items as review candidates rather than
automatic additions. Attachments and notes are supporting source material, not
independent research-information records unless a reviewer promotes one as a
first-class document.

## Current inventory

The public API snapshot fetched at `2026-08-12T00:03:17Z` records library
version `451` and normalized payload digest
`5e0f5fe1d68c18214110a37c24a8e9177dc484f64a1d9d832f322b477bfef20d`:

- 197 top-level items;
- 127 memberships in `CON Articles`;
- 55 in excluded `External`;
- 4 in `CON Datasets`;
- 3 in `CON Zenodo/OSF DOIs`;
- 0 in `CON Software`; and
- 8 unfiled top-level items.

The current snapshot contains six normalized DOI collision groups. The
deterministic completeness rule selects one preferred source item and retains
the supporting Zotero item identifiers; conflicting fields remain explicit in
the candidate report.

## Target mapping

| Zotero information | Target |
| --- | --- |
| Journal article, conference paper, preprint, book section, report, thesis | `XYZPublication.json` |
| Dataset or registry-classified dataset | `XYZDataset.json` |
| Computer program or registry-classified software | `XYZInstrument.json`, kind `obo:IAO_0000010` |
| Genuinely generic document | `XYZDocument.json` |
| Publication venue referenced by accepted publications | `XYZPublicationVenue.json` |
| Creators not already represented | `XYZPerson.json` or `XYZOrganization.json` after reconciliation |

Generic Zotero `document` items require enrichment from Crossref or DataCite
and collection context before class assignment.

## Refresh contract

The adapter:

1. fetch all collection and top-level item pages at one verified library
   version, with requested and returned API versions recorded;
2. exclude deleted, child, and `External` records from automatic publication;
3. normalize DOI, ISBN, PMID, PMCID, ORCID, and URL identifiers;
4. queues records that still require Crossref or DataCite enrichment rather
   than guessing a class;
5. reconcile against existing records by canonical identifier;
6. render stable, sorted candidate arrays;
7. validate candidates against the configured research-information collection;
8. present additions, changes, removals, collisions, and uncertain mappings for
   human review; and
9. updates this directory only through an explicit promotion command.

Acquisition and transformation run through checked-in Pixi tasks. Zotero item
keys and the library version must remain available as source
identifiers/provenance even when a DOI becomes the entity `pid`.

## Current implementation

Generate a source snapshot and review candidates with:

```sh
pixi run zotero-refresh
```

Candidate `XYZ*.json` files are written to
`build/zotero_centerforopenneuroscience/`; the reconciliation report is written
beside that directory. Nothing is promoted to this directory automatically.

## Validated refresh

The version `451` snapshot deterministically generates 153 records, all of
which pass the configured research-information validator:

- 4 `XYZDataset` records;
- 3 `XYZInstrument` records;
- 126 `XYZPublication` records; and
- 20 `XYZPublicationVenue` records.

The source-scoped records intentionally reuse canonical PIDs already seen in
other sources: five publications overlap `con_site`, and thirteen venues overlap
the shared psychoinformatics pool. These are the same entities, not new IDs.
Multi-source consumers must reconcile records by PID and apply an explicit
field-level merge policy.

The refresh report also identifies 1,817 unresolved creator occurrences across
1,221 names, 49 unmapped tag occurrences across 36 values, and 42 venue names
without ISSNs. Their source information remains in the committed Zotero
snapshot for later registry enrichment and review; it is not converted into
unsafe local identities.
