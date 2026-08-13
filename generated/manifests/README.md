# Consumer manifests

`full-fidelity.json` is the active, executable inventory for this flattened consumer.
It declares all accepted Milestone 3 content and explicitly uses an unfiltered `all` selection policy.

`source-import.json` maps every one of the 500 accepted overlay/configuration source-tree entries (495 profile entries plus five CON Hugo configuration files) and the accepted Congo theme gitlink to this layout. Ordinary content is byte-identical except for two documented configuration path/identity transforms.
The sixteen former annex symlinks are intentionally represented by digest-verifiable read-only hydration contracts instead.

`zotero-import.json` maps the complete reviewed public Zotero snapshot, policies, candidates, tools, and test evidence from the pinned migration repository commit.

`framework-import.json` maps all 72 accepted presentation files from the source site's `config/_default`, `archetypes`, `layouts/`, `assets/`, and `static/` trees.
Fifty-nine are byte-identical copies of ordinary source blobs.
Thirteen source blobs were git-annex pointer paths, so the consumer instead carries the exact payload bytes verified from the allowed, hydrated `leej3/www-from-model` mirror at its recorded commit.
For each materialization, the manifest preserves the source blob identity and pointer digest, annex key, payload size and MD5, ordinary-Git target SHA-256, and mirror provenance.
This framework packaging correction is distinct from the sixteen site asset hydration contracts: the framework runtime requires no git-annex operation.
The presentation files remain in the consumer because their absent upstream license prevents runtime-release redistribution.

`theme-import.json` separately maps all 467 files from the pinned Congo theme repository commit and records its MIT license.
The accepted site represented the theme as a gitlink; the consumer stores only ordinary files.

`projection-input-import.json` maps the accepted projection contract, all nine Jinja templates, the graph producer, and the historical v1 digest ledger.
The active contract is flattened to `site/projection.yaml`; the engine generates and verifies a new v2 digest at `generated/projection/SHA256SUMS`.
The active contract also declares the accepted per-class selection, inline expansion, recursive project closure, and reverse-link injection semantics; none of that CON-specific policy is implicit in the generic engine.

`milestone-3/` and `generated/projection/provenance/` preserve the original assembly/projection checksums and source contracts. They contain historical source-coordinate paths such as `profiles/con`; they are provenance, not active consumer path configuration.
