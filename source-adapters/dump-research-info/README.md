# dump-research-info source adapter

This site-specific source adapter compares the legacy `data/con_site` JSON records in a caller-provided `dump-research-info` Git checkout with the Things in this site's `metadata/records/` tree.
It reports exact PID and identifier matches, source-only candidates, possible field enrichments, unresolved relationship targets, and known placeholder values.

The adapter never clones, fetches, checks out, or modifies its source.
DataLad is expected to provide the exact checkout and record its revision.
Read-only review never changes canonical metadata. The explicit materialization
command synchronizes transformed source values into `metadata/records/**` for human
review.

## Read-only review

From this repository, inspect a checkout supplied by the caller:

```console
./source-adapters/metadata/metadata-review review -- \
  --only dump-research-info \
  --source-input dump-research-info=/path/to/dump-research-info
```

Ignored artifacts are written below `build/metadata-review/dump-research-info/review/`.

## Generate the metadata change

After this adapter code has merged, create a clean downstream branch and keep the `dump-research-info` checkout at the revision to be reviewed.
Resolve both input revisions into the literal generator command recorded by DataLad:

```console
(
  set -eu

  SOURCE=../orinoco-lite-dev/submodules/dump-research-info
  test -z "$(git status --porcelain=v1 --untracked-files=all)"
  test -z "$(git -C "$SOURCE" status --porcelain=v1 --untracked-files=all)"
  SOURCE_COMMIT=$(git -C "$SOURCE" rev-parse HEAD^{commit})
  DOWNSTREAM_COMMIT=$(git rev-parse HEAD^{commit})

  pixi run datalad run --explicit \
    -m "review dump-research-info con_site at ${SOURCE_COMMIT:0:12}" \
    -i source-adapters/dump-research-info/metadata_adapter.py \
    -i metadata/records \
    -o metadata/records \
    "python source-adapters/dump-research-info/metadata_adapter.py \
      --materialize \
      --source '$SOURCE' \
      --downstream . \
      --expected-source-commit '$SOURCE_COMMIT' \
      --downstream-revision '$DOWNSTREAM_COMMIT'"
)
```

The command uses DataLad from this downstream's committed root Pixi lock in an ordinary Git repository; no large-file backend is installed or required.
The fail-fast preflight requires clean source and downstream checkouts.
The adapter independently verifies the exact source revision and source cleanliness, while the recorded downstream revision and DataLad commit parent identify the metadata input.
`datalad run` executes in this ordinary downstream Git repository and writes native YAML additions and replacements, including field removals, directly below `metadata/records/**`; DataLad creates the provenance-bearing data commit on the current branch.
For every source record it can identify unambiguously, the adapter treats the transformed source representation as the proposed state.
It may add, remove, or change values in matched site records; the resulting Git diff is the review surface.
Site records absent from this source are unchanged by this adapter run.
Conflicts and unresolved relationships remain visible in the diff or normal validation results rather than being filtered out.
Its detailed comparison report remains ignored below `build/metadata-review/dump-research-info/materialize/`.
No custom committed review schema, `.datalad` metadata, submodule, copy step, or second provenance repository is introduced.

Inspect that generated commit, then reproduce it through the same locked Pixi environment:

```console
pixi run datalad rerun HEAD
```

The source checkout path recorded by DataLad is project-relative, and the checkout must still be at the commit embedded in the recorded command.
After the rerun produces no changes, submit the DataLad commit as the metadata pull request.
Its diff is the proposed canonical data modification itself; provenance lives in the DataLad commit rather than in a second committed evidence format.
Human review and the normal validation/projection gates still determine whether any generated value is accepted.
