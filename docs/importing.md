# Importing a complete site bundle

The template is intentionally content-neutral.
`import_site_bundle.py` moves a prepared, reviewed site's complete visible inputs into the ordinary downstream layout without importing its Git ancestry or framework topology.

The source bundle may contain every path classified as site-owned or replaceable by `template-ownership.yml`, including:

- `metadata/`, including records, reference closure, and provenance;
- `editorial/`;
- `assets/`;
- `site/`;
- `integrations/`;
- `extensions/`;
- the committed `generated/` projection and its provenance;
- site-owned browser, integration, and parity tests; and
- site policy files such as a reviewed `LICENSE` or `CITATION` file.

`orinoco.yaml` is initialized-site-owned and can also be imported, but replacing the generated starting configuration requires the explicit `--replace` review step.

An optional root `orinoco-site-bundle.json` provides a format identifier, source provenance, and an exact SHA-256 for every source payload file.
It may additionally declare classifications, sizes, and summary counts; the importer verifies all supplied fields before writing and preserves the manifest itself as site-owned import evidence.
The manifest is intentionally not a member of its own inventory.
The post-import `metadata/provenance/site-import-*.json` ledger and the template facade are also outside that source inventory, so the declared source count remains stable after import.
Downstream checks must verify the paths declared by the preserved manifest rather than recomputing an inventory over template-owned files added at the destination.
Without a manifest, `--source-repository`, `--source-commit`, and `--scope` are required on the command line.

Existing non-placeholder files are never overwritten unless `--replace` is given.
Even with `--replace`, `.git` components, `.gitmodules`, repository-root `.github/` workflows, template-owned documentation and tools, credentials, and symlinks are rejected.
Inert nested `.github/` metadata belonging to a classified site-owned vendor tree, such as `site/framework/themes/congo/.github/`, is allowed without granting it root workflow ownership.
The result is a site-owned import ledger under `metadata/provenance/`.
