# Historical CON site metadata gap

This directory contains review evidence produced by a one-off DataLad run.
It compares `con/dump-research-info:data/con_site` at commit `397b5608` with this downstream's canonical and reference metadata at commit `6cc26739`.

The extraction found 19 source-only candidates and possible field-level enrichment for 60 matched records.
These are historical source assertions, not approved current facts or schema-valid canonical records.
In particular:

- the two person candidates are Brock Wester and Russell Poldrack, whose public visibility remains a human decision;
- two grant candidates contain explicitly documented fabricated NIH Reporter URLs and must not be promoted as written;
- source-only organizations, publications, venues, and grants still require content, licensing, relationship, and current-schema review; and
- the enrichment files distinguish absent fields from fields whose source and downstream values differ, but make no choice between them.

`provenance.json` binds the copied ordinary files to the DataLad run commit, exact input commits, extractor digest, and output tree.
A verified DataLad rerun produced the identical output tree.

Nothing here is loaded by the site or promoted into `metadata/records/**`.
