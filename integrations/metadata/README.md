# Experimental metadata source adapters

This site-owned integration is the first proof of the Orinoco metadata-adapter
goal.
It is intentionally downstream-specific and is not yet a template interface.

Run a read-only review of every configured live source against its committed
source evidence, transformed candidates, and canonical YAML:

```console
./integrations/metadata/metadata-review review
```

The command writes ignored JSON, Markdown, fetched-source, candidate, and
canonical-impact artifacts below `build/metadata-review/`.
It never changes tracked files.

After inspecting that report, explicitly refresh only the committed source
snapshot, deterministic source candidates, and aggregate provenance ledger:

```console
./integrations/metadata/metadata-review refresh-evidence
```

The wrapper requires Pixi and always uses the committed lock plus a detached
environment. This keeps executables and symlinks out of the site-owned
`integrations/` tree that Orinoco validates and packages. Pixi creates a
temporary workspace link to that detached environment; the wrapper removes
that link on every exit while retaining the cached environment itself.

That command still does not change `metadata/records/**`.
Commit the evidence refresh on a dedicated branch, run the complete consumer
test suite, and open an ordinary metadata-review pull request.
A reviewer may then promote selected candidates in a separate content commit,
refresh the projection, and review the rendered impact.

The experimental host loads the modules declared in `sources.toml`.
Adapters and their source policy remain site-owned.
The host contract must be exercised by at least two independent adapters and a
real review pull request before any common facade is proposed for the template.
