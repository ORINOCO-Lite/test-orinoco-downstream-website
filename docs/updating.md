# Framework updates

Update the template and package through one review pull request. During
development, select exact Git commits so the combination can be tested before
either component is released.

## Apply selected commits

Start from a clean branch. Run Copier with the selected template commit and,
when needed, explicit package coordinates:

```console
pixi run copier update --defaults --trust \
  --vcs-ref <template-commit> \
  --data package_repository=https://github.com/ORINOCO-Lite/orinoco-lite-dev.git \
  --data package_revision=<package-commit>
```

The package repository may instead be a suitable fork. A release tag is also a
valid revision, but a release is not required for testing or adoption.

Copier updates `.copier-answers.yml`, the template-owned scaffold, and the
normal `pixi.lock`. The site-owned `site-specific/` and `extensions/` trees
must remain unchanged unless the pull request explicitly includes a reviewed
site change.

## Check the result

Inspect the complete diff and make sure Copier did not leave a rejected patch:

```console
find . -name '*.rej' -print
git diff --check
git diff -- site-specific extensions
pixi run --frozen validate
pixi run --frozen verify-build
```

The first and third commands normally produce no output. Resolve any reported
conflict before committing. Push the focused update branch and let the
repository's normal pull-request checks run. Approval and merge remain human
gated.

Deferring an update leaves the default branch unchanged. Roll back a merged
update with a reviewed revert of its update commit; do not move a release tag
or silently replace a selected commit.
