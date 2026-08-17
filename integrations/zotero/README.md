# Public Zotero integration

This directory retains the complete reviewed Milestone 3 Zotero evidence from
`dump-research-info` commit
`062da59cb5a00ca128b3df895426a54088bfc625`.

- `source/` contains the byte-identical public API snapshot and deterministic
  candidate class files.
- `policy/` contains the reviewed creator, addition, migration, and merge
  policies.
- `tools/` contains the two read-only acquisition/transformation tools adapted
  to the flattened repository layout.

The active tools may read the public Zotero API and write review candidates to
local build state. They cannot write to Zotero or promote a candidate into
`metadata/records`. Promotion is a separate, human-reviewed content change.

Git history records changes to this integration and its source snapshot.
