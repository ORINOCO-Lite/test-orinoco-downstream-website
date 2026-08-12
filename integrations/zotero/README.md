# Public Zotero integration

This directory retains the complete reviewed Milestone 3 Zotero evidence from
`dump-research-info` commit
`062da59cb5a00ca128b3df895426a54088bfc625`.

- `source/` contains the byte-identical public API snapshot and deterministic
  candidate class files.
- `policy/` contains the reviewed creator, addition, migration, and merge
  policies.
- `provenance/` contains the human review notes plus exact source copies of the
  upstream tools and all 42 upstream test definitions.
- `tools/` contains the two read-only acquisition/transformation tools adapted
  to the flattened repository layout.

The active tools may read the public Zotero API and write review candidates to
local build state. They cannot write to Zotero or promote a candidate into
`metadata/records`. Promotion is a separate, human-reviewed content change.

The source copy of `zotero_reviewed_additions.py` is retained only as historical
evidence because it includes an authenticated `--apply` mode. It has the
`.source` suffix and is not part of the supported consumer command surface.

`generated/manifests/zotero-import.json` records the source-to-target mapping,
hashes, library version, and candidate counts.
