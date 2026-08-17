---
name: manage-orinoco-content
description: Edit and review an Orinoco Lite downstream's human-authored editorial content and declared assets. Use when adding or changing files under custom/editorial or custom/assets, updating asset declarations, reviewing focused content diffs, or preparing a site content pull request. The integrations interface is intentionally provisional.
---

# Manage Orinoco content

Work only in the downstream's user-facing source layer. Keep generated output,
tool state, and migration evidence out of content commits.

## Editorial workflow

1. Read `site/presentation.yaml` to understand where editorial files are used.
2. Edit Markdown under `custom/editorial/`; preserve existing front matter and
   navigation intent.
3. Do not edit `generated/`. Run `pixi run validate` to regenerate it locally.
4. Run `pixi run build` and inspect the affected page before committing.
5. Keep the commit focused on source files; ignored projection output is not
   review evidence.

## Asset workflow

1. Put site-managed payloads under `custom/assets/files/`.
2. Update `custom/assets/manifest.yaml` when an asset is part of the declared
   build contract.
3. For committed payloads, use ordinary Git. Do not initialize git-annex, add
   annex pointer rules, or introduce a large-file backend.
4. For payloads fetched from an external source, record only the immutable URL,
   byte size, and SHA-256 needed to verify that external fact. Do not duplicate
   Git blob or commit identity in a separate inventory.
5. Run `pixi run assets-verify`, `pixi run validate`, and `pixi run build`.

## Boundaries

- Treat `metadata/records/`, `metadata/reference/`, `custom/`, `site/`, and
  `extensions/` as user-facing source.
- Treat `.orinoco-lite/` as implementation support. Change it only for an
  explicit framework-maintenance task.
- Never commit `generated/`, `.orinoco-lite/state/`, caches, build output, or a
  second digest inventory of the same commit.
- Prefer a small source diff plus rendered review over provenance narration in
  the downstream tree.

## Integrations

TODO: Define the stable adapter contract after the current source adapters have
been exercised and reviewed. Until then, read the integration's own README and
tests, keep generated evidence inside that integration, and do not generalize
one adapter's output format into a downstream-wide contract.
