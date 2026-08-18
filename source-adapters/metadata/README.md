# Experimental metadata source adapters

This site-specific source adapter is the first proof of the Orinoco source-adapter goal.
It is intentionally downstream-specific and is not yet a template interface.

Run a read-only review of every configured live source against its committed source evidence, transformed candidates, and canonical YAML:

```console
./source-adapters/metadata/metadata-review review
```

The command writes ignored JSON, Markdown, fetched-source, candidate, and canonical-impact artifacts below `build/metadata-review/`.
It never changes tracked files.

After inspecting that report, explicitly refresh only the committed source snapshot and deterministic source candidates:

```console
./source-adapters/metadata/metadata-review refresh-evidence
```

The wrapper requires Pixi and always uses the committed lock plus a detached environment.
This keeps executables and symlinks out of the repository's `source-adapters/` tree that Orinoco validates and packages.
Pixi creates a temporary workspace link to that detached environment; the wrapper removes that link on every exit while retaining the cached environment itself.

That command still does not change `metadata/records/**`.
Commit the evidence refresh on a dedicated branch, run the complete consumer test suite, and open an ordinary metadata-review pull request.
A reviewer may then promote selected candidates in a separate content commit, refresh the projection, and review the rendered impact.

The experimental host loads the modules declared in `sources.toml`.
Adapters and their source policy remain site-specific and live in this repository.
The host contract must be exercised by at least two independent adapters and a real review pull request before any common facade is proposed for the template.

Adapters that require a caller-pinned checkout are opt-in.
Select one and pass its input explicitly after Pixi's argument delimiter:

```console
./source-adapters/metadata/metadata-review review -- \
  --only dump-research-info \
  --source-input dump-research-info=/path/to/dump-research-info
```

See [`../dump-research-info/README.md`](../dump-research-info/README.md) for the in-repository `pixi run ... datalad run` command that records provenance while producing an ordinary canonical metadata diff.
Direct `refresh-evidence` is deliberately blocked for that adapter: canonical materialization must be performed by the provenance-bearing DataLad task.
