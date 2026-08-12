# File ownership

`template-ownership.yml` is the executable ownership contract.

Copier owns the small framework facade: workflows, commands, updater, ownership verifier, and these contract documents.
If a downstream edit to one of those files overlaps a template update, Copier writes a `.rej` conflict and the update stops for human review.

The site owns `orinoco.yaml`, every canonical and reference record, provenance, editorial content, assets, presentation overlay, integration evidence, and extensions.
Copier creates those paths once and excludes them from all later updates.
The updater hashes them before and after every run and rejects an undeclared change.
It applies the same byte-for-byte protection to the complete committed `generated/` projection; only `generated/manifests/framework-update.json` may change as updater bookkeeping.

`orinoco.lock` is structured release state.
The updater may change exact engine, runtime, template, and workflow pins; its diff is always part of the update pull request.
`generated/` is replaceable only after declared inputs validate.
For a concrete release, `pixi.toml` appends the reviewed `orinoco.lock` SHA-256 as a `#sha256=` fragment on the exact wheel URL.
Pixi 0.73 preserves that fragment in the `pixi.lock` package's `direct+` URL and version, but does not duplicate it in a separate `sha256` field.
Ownership verification therefore checks the exact manifest hash fragment and locked direct URL/version together.
The ownership verifier checks all three and, when installed, also checks the distribution metadata version.

Semantic content changes are never smuggled into a framework update.
If a schema change genuinely requires one, the update must name a migration, list the allowed site-owned paths, isolate the semantic diff, and leave the ledger in `human-review` status.
