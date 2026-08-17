# Center for Open Neuroscience — Orinoco Test Site

Test-only full-content Orinoco Lite downstream for the Center for Open Neuroscience.

This is an ordinary single-repository Orinoco Lite site.
Canonical metadata, editorial content, declared assets, integration evidence, and supported local extensions are versioned directly here.
Building, previewing, and updating the site does not require Git submodules or knowledge of the Orinoco engineering workspace.

## Commands

```console
pixi run validate
pixi run projection-update
pixi run projection-verify
pixi run assets-hydrate
pixi run assets-verify
pixi run build
pixi run serve
pixi run test
pixi run test-all
pixi run update-check
pixi run update-orinoco -- --to-template v0.2.0 --to-engine 0.2.0
```

The checked `orinoco.lock` is the release authority.
The template is released with exact published engine, runtime, workflow, and frozen Pixi coordinates.
Updates produce focused lock and template-facade diffs; they never merge themselves.

The default installs the engine wheel from an exact immutable release URL.
PyPI distribution is optional and remains a separate release/license decision.

After editing canonical metadata, `pixi run validate` regenerates the ignored
projection and validates the resulting records, pages, and graph. `pixi run
build` does the same before building. The source commit therefore shows the
metadata change rather than a duplicate generated tree.
`pixi run assets-hydrate` is the explicit networked step for declared remote assets.
Once warmed, `pixi run assets-verify` confirms that the checked manifest and local payloads are complete without silently fetching them.
The ordinary online `pixi run test-all` gate runs those two phases in that order through `assets-prepare-online`, so it also works from a cold clone.

`pixi run test-all` is the complete acceptance gate: configuration, projection, and runtime validation, exact Hugo Extended 0.154.5, all consumer tests, two independent byte-compared static builds, and the checked Chromium/WebKit browser scenarios.
`pixi run build` emits host-neutral root-relative links, so the same local artifact works at both `http://127.0.0.1:8765/` and `http://localhost:8765/` when served with `pixi run serve`.
`pixi run test-all` verifies both loopback names against that one artifact.
Pages and browser-project builds retain their separate explicit project-path base URLs and public canonical metadata.
The browser installer runs the checked npm lock unchanged.
On macOS 14 only, it overlays Playwright 1.61.1 in `node_modules` to match WebKit revision 2251 without the newer `PushAPIEnabled` protocol request, then verifies `@playwright/test`, `playwright`, and `playwright-core`.
The overlay uses `--no-save`, `--package-lock=false`, and `--ignore-scripts`; it restores and fails if npm changes either consumer-owned package input.
Other platforms continue to use the checked Playwright 1.62.1.

## Network boundary

Hydration is the only asset command authorized to retrieve declared read-only payload URLs.
For the warmed-cache offline proof, run `pixi run assets-hydrate` while online, deny network access at the operating-system boundary, and then run `pixi run assets-verify` before the offline validation, projection, build, and editor checks.
Do not use `assets-prepare-online` for that denied-network phase: it deliberately represents the normal cold-clone preparation path.

## Content

- `metadata/records/` contains canonical YAML records.
- `metadata/reference/` contains the explicit reference closure.
- `.orinoco-lite/` contains implementation support used by the checked commands.
- `custom/editorial/`, `custom/assets/`, and `site/` contain site-owned
  presentation inputs.
- `.agents/skills/manage-orinoco-content/` guides agents through focused
  editorial and asset changes.
- `integrations/` contains optional, read-only source-ingestion evidence and tools; it is not a deployed runtime dependency.
- `extensions/` is the stable downstream customization surface.
- `generated/` contains ignored projection output recreated by validation and
  builds.

See [ownership](docs/ownership.md), [updates](docs/updating.md), and the
[site operating guide](site/README.md).
