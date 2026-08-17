# Accepted presentation snapshot

This directory contains the complete `config/_default`, `archetypes`, `layouts/`, `assets/`, and `static/` presentation snapshot reachable from accepted site commit `26907c487efaa2c31bba9d02398aa201ab6f774b`.

They live in the consumer because the Orinoco Lite runtime release cannot redistribute this upstream tree without an explicit license.
Their inclusion here preserves the already-reviewed site inputs and does not assert a new license.
All presentation files are ordinary Git blobs, including the compiled stylesheet and images needed for a complete local build.

`themes/congo/` is flattened separately from the site commit's gitlink.
Its 467 files come from `https://github.com/leej3/congo.git` commit `3623fa505ee42fee899844d94a4ff7f5a1ae9096`, including that repository's MIT `LICENSE`.
The consumer contains no gitlink.

The mounts in `site/config/module.toml` put this snapshot below the CON-owned
overrides in `custom/assets/files`, `site/layouts`, `site/static`, and the
generated projection.
Framework updates must present changes to this directory as an ordinary human-reviewed downstream diff.
Framework changes are reviewed as ordinary source diffs.
