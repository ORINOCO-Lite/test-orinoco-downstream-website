# dump-research-info metadata adapter

This site-owned adapter compares the legacy `data/con_site` JSON records in a caller-provided `dump-research-info` Git checkout with this downstream's canonical and reference YAML.
It reports exact PID and identifier matches, source-only candidates, possible field enrichments, unresolved relationship targets, and known placeholder values.

The adapter never clones, fetches, checks out, or modifies its source.
DataLad is expected to provide the exact checkout and record its revision.
The adapter also never promotes values into `metadata/records/**`.

## Read-only review

From this repository, inspect a checkout supplied by the caller:

```console
./integrations/metadata/metadata-review review -- \
  --only dump-research-info \
  --source-input dump-research-info=/path/to/dump-research-info
```

Ignored artifacts are written below `build/metadata-review/dump-research-info/review/`.

## Generate the evidence commit

After this adapter code has merged, create a clean downstream branch and keep the `dump-research-info` checkout at the revision to be reviewed.
Resolve both input revisions into the literal generator command recorded by DataLad:

```console
SOURCE=/path/to/dump-research-info
test -z "$(git status --porcelain=v1 --untracked-files=all)"
test -z "$(git -C "$SOURCE" status --porcelain=v1 --untracked-files=all)"
SOURCE_COMMIT=$(git -C "$SOURCE" rev-parse HEAD^{commit})
DOWNSTREAM_COMMIT=$(git rev-parse HEAD^{commit})

pixi run \
  --config-file integrations/metadata/pixi-config.toml \
  --manifest-path integrations/metadata/pixi.toml \
  datalad run --explicit \
  -m "review dump-research-info con_site at ${SOURCE_COMMIT:0:12}" \
  -i integrations/dump-research-info/metadata_adapter.py \
  -i integrations/metadata \
  -i metadata/records \
  -i metadata/reference \
  -o integrations/dump-research-info/source/con-site-gap \
  "./integrations/metadata/metadata-review \
    extract-dump-research-info -- \
    --source '$SOURCE' \
    --expected-source-commit '$SOURCE_COMMIT' \
    --downstream-revision '$DOWNSTREAM_COMMIT' \
    --output integrations/dump-research-info/source/con-site-gap"
```

The command uses DataLad and git-annex from this integration's committed Pixi lock.
The adapter requires the source checkout to be clean and fail-closes if either recorded revision no longer describes its input.
`datalad run` executes in this ordinary downstream Git repository and writes only `integrations/dump-research-info/source/con-site-gap/**`; DataLad creates the provenance-bearing commit directly on the current branch.
No `.datalad` metadata, submodule, copy step, or second provenance repository is introduced.

Inspect that generated commit, then reproduce it through the same locked Pixi environment:

```console
pixi run \
  --config-file integrations/metadata/pixi-config.toml \
  --manifest-path integrations/metadata/pixi.toml \
  datalad rerun HEAD
```

The source checkout must still be at the commit embedded in the recorded command.
After the rerun produces no changes, submit the DataLad commit as the separate metadata-evidence pull request.
It must not modify the adapter, common host, or canonical metadata.
Any selected canonical promotion belongs in a later content pull request.
