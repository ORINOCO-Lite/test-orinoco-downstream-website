# Framework updates

Check for an update without changing the checkout:

```console
pixi run update-check
```

Apply reviewed exact releases:

```console
pixi run update-orinoco -- \
  --to-template v0.2.0 \
  --to-engine 0.2.0 \
  --engine-url https://github.com/example/releases/download/v0.2.0/orinoco_lite-0.2.0-py3-none-any.whl \
  --engine-sha256 <64-hex-digest> \
  --to-runtime 0.2.0 \
  --runtime-url https://github.com/example/releases/download/v0.2.0/runtime.tar.gz \
  --runtime-sha256 <64-hex-digest> \
  --runtime-manifest-sha256 <64-hex-digest> \
  --workflow-sha <40-hex-commit> \
  --workflow-ref owner/repository/.github/workflows/orinoco-consumer-ci.yml@<40-hex-commit>
```

The updater requires a clean Git worktree, snapshots every site-owned file and every existing generated projection byte, runs Copier with `.rej` conflicts, updates `orinoco.lock`, refreshes `pixi.lock`, and writes `generated/manifests/framework-update.json`.
That update ledger is the only generated path exempt from the before/after byte comparison.
It stops if site content changed, a template conflict exists, or a pin is incomplete.

Template versions are exact tags backed by immutable GitHub Releases.
The updater resolves the current and target tags to their peeled 40-hex commits, resolves the target again after Copier finishes, rejects an unavailable or moving tag, and records both resolved commits in the update ledger.
The commit is evidence resolved at update time; it is not baked into `.copier-answers.yml` or `orinoco.lock`, because a release tree cannot contain its own commit ID.

An update can be deferred by leaving the pull request open or closing it.
To roll back a merged update, revert the update commit so the previous `orinoco.lock`, `.copier-answers.yml`, template files, and generated outputs are restored together.
No canonical content rollback is needed unless the reviewed update explicitly contained a semantic migration.

The scheduled workflow opens a pull request; it never merges automatically.
Security releases use the `security` classification so their urgency is visible, without bypassing review.
