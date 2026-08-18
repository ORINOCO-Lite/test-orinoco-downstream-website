---
name: manage-orinoco-content
description: Edit and review an Orinoco Lite downstream's human-authored editorial content, declared assets, and site-owned source adapters. Use when changing files under custom/editorial, custom/assets, or source-adapters; updating asset declarations; reviewing focused source diffs; or preparing a site content pull request. The shared source-adapter interface is intentionally provisional.
---

# Manage Orinoco content

Work only in the downstream's user-facing source layer.
Keep generated output, tool state, and migration evidence out of content commits.

## Editorial workflow

1. Read `site/presentation.yaml` to understand where editorial files are used.
2. Edit Markdown under `custom/editorial/`; preserve existing front matter and navigation intent.
3. Do not edit `generated/`.
Run `pixi run validate` to regenerate it locally.
4. Run `pixi run build` and inspect the affected page before committing.
5. Keep the commit focused on source files; ignored projection output is not review evidence.

## Asset workflow

1. Put site-managed payloads under `custom/assets/files/`.
2. Update `custom/assets/manifest.yaml` when an asset is part of the declared build contract.
3. For committed payloads, use ordinary Git.
Do not initialize git-annex, add annex pointer rules, or introduce a large-file backend.
4. For payloads fetched from an external source, record only the immutable URL, byte size, and SHA-256 needed to verify that external fact.
Do not duplicate Git blob or commit identity in a separate inventory.
5. Run `pixi run assets-verify`, `pixi run validate`, and `pixi run build`.

## Boundaries

- Treat `metadata/records/`, `custom/`, `site/`, `source-adapters/`, and `extensions/` as user-facing source.
- Treat `.orinoco-lite/` as implementation support.
Change it only for an explicit framework-maintenance task.
- Never commit `generated/`, `.orinoco-lite/state/`, caches, build output, or a second digest inventory of the same commit.
- Prefer a small source diff plus rendered review over provenance narration in the downstream tree.

## Source adapters

TODO: Define the stable shared interface after multiple source adapters have been exercised and reviewed.
Until then, keep each concrete adapter under `source-adapters/<name>/`, read its own README and tests, and do not generalize one adapter's modes or report format into a downstream-wide contract.

Upstream tools use the concrete roles importer, enricher, and scraper.
`source adapter` is Orinoco Lite's local umbrella, and `report` may be an adapter mode; do not present either as an upstream plugin interface.
Its documentation must say which site metadata it reads and writes.
Every Thing that participates in projection belongs under `metadata/records/`.
Adapter-only matching data belongs with that adapter, not under `metadata/`.
Generic template-managed adapters and their support data belong under `.orinoco-lite/source-adapters/`; site-owned adapter configuration remains at the repository root.
Prefer released upstream acquisition, matching, query, and enrichment helpers.
Use upstream PAV source annotations where they add useful attribution, but do not impose a universal field-ownership gate during the prototype: a documented mode may propose arbitrary changes and pull-request review remains authoritative.
Review the ordinary Git diff for every run; generated reports belong under ignored build output rather than beside the user-facing metadata records.
