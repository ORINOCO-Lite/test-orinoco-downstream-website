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

Before changing the checkout, the updater independently renders the current and
target template releases. A pre-applied template-owned bootstrap edit is accepted
only when a three-way merge of the downstream file, current release, and target
release produces the exact target bytes and mode, or when the downstream file is
byte-for-byte an exact stable intervening template release of that same
template-owned path. This permits a reviewed bootstrap from an earlier immutable
release to advance without treating arbitrary framework edits as safe. All
site-owned or generated changes still stop visibly. If Copier reintroduces a
template `.gitkeep` into an already populated protected directory, the updater
removes only that new placeholder; placeholders in empty directories and every
pre-existing protected byte remain unchanged.

Consumers created before template v0.1.3 must first copy
`copier-template/tools/update_orinoco.py` from the exact reviewed target tag to
`tools/update_orinoco.py` byte-for-byte and commit that single bootstrap file.
This narrow handoff is necessary because an older updater cannot implement the new
three-way proof itself. Do not bootstrap any site-owned or generated path, and do
not combine arbitrary framework customization with this one-time updater sync.

Template versions are exact tags backed by immutable GitHub Releases.
The updater resolves the current and target tags to their peeled 40-hex commits, resolves the target again after Copier finishes, rejects an unavailable or moving tag, and records both resolved commits in the update ledger.
The commit is evidence resolved at update time; it is not baked into `.copier-answers.yml` or `orinoco.lock`, because a release tree cannot contain its own commit ID.

An update can be deferred by leaving the pull request open or closing it.
To roll back a merged update, revert the update commit so the previous `orinoco.lock`, `.copier-answers.yml`, template files, and generated outputs are restored together.
No canonical content rollback is needed unless the reviewed update explicitly contained a semantic migration.

The update workflow runs only by explicit `workflow_dispatch`.
Before dispatching it, review an immutable release and enter its exact template,
engine, runtime, and reusable-workflow coordinates and digests.
It opens a pull request and never merges automatically.
There is no scheduled release discovery until a separately reviewed mechanism can
discover complete immutable coordinates without relying on mutable aliases or empty
workflow inputs.
Security releases use the `security` classification so their urgency is visible, without bypassing review.
