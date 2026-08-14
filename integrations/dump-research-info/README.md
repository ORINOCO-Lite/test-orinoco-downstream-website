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

After this adapter code has merged, create a clean downstream branch and run:

```console
./integrations/metadata/metadata-review \
  datalad-run-dump-research-info -- \
  --source /path/to/dump-research-info
```

The task uses DataLad and git-annex from this integration's committed Pixi lock.
It requires both checkouts to be clean, resolves the exact source and downstream commits, and executes `datalad run` in this ordinary downstream Git repository.
The recorded command writes only `integrations/dump-research-info/source/con-site-gap/**`; DataLad creates the provenance-bearing commit directly on the current branch.
No `.datalad` metadata, submodule, copy step, or second provenance repository is introduced.

Inspect that generated commit, then reproduce it through the same detached environment:

```console
./integrations/metadata/metadata-review datalad-rerun -- HEAD
```

The source checkout must still be at the commit embedded in the recorded command.
After the rerun produces no changes, submit the DataLad commit as the separate metadata-evidence pull request.
It must not modify the adapter, common host, or canonical metadata.
Any selected canonical promotion belongs in a later content pull request.
