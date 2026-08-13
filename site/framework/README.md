# Accepted presentation snapshot

This directory contains the complete `config/_default`, `archetypes`, `layouts/`, `assets/`, and `static/` presentation snapshot reachable from accepted site commit `26907c487efaa2c31bba9d02398aa201ab6f774b`.

They live in the consumer because the Orinoco Lite runtime release cannot redistribute this upstream tree without an explicit license.
Their inclusion here preserves the already-reviewed site inputs and does not assert a new license.
Fifty-nine files are byte-identical source blobs.
The other thirteen source blobs were git-annex pointer paths rather than presentation bytes; those targets are verified annex payload materializations stored here as ordinary Git files so builds do not require git-annex.
The payloads came from the allowed, hydrated `leej3/www-from-model` mirror at commit `6c8b9a5b7260dc20dfe1453dd863b353e8f90f06` without modifying that checkout.
`generated/manifests/framework-import.json` preserves every original source blob and pointer digest and records each annex key, payload size and MD5, ordinary-Git target SHA-256, and materialization provenance.

`themes/congo/` is flattened separately from the site commit's gitlink.
Its 467 files come from `https://github.com/leej3/congo.git` commit `3623fa505ee42fee899844d94a4ff7f5a1ae9096`, including that repository's MIT `LICENSE`.
`generated/manifests/theme-import.json` preserves the distinct theme provenance and every source blob digest.
The consumer contains no gitlink.

The mounts in `site/config/module.toml` put this snapshot below the CON-owned overrides in `assets/files`, `site/layouts`, `site/static`, and the generated projection.
Framework updates must present changes to this directory as an ordinary human-reviewed downstream diff.
Source and built-output inventory checks fail closed if a pointer-form regular file is introduced.
