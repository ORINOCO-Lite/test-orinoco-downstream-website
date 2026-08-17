# File ownership

`.orinoco-lite/template-ownership.yml` is the executable ownership contract.

| Class | Who changes it | Examples |
| --- | --- | --- |
| `template_owned` | Copier, with three-way conflict handling | workflows, command facade, updater, verifier, and generic docs |
| `initialized_site_owned` | The site after one-time creation | `orinoco.yaml`, metadata, `custom/`, `site/`, and integrations |
| `engine_lock` | The pinned updater, as a reviewed structured diff | `orinoco.lock` and `pixi.lock` |
| `extensions` | The site | stable custom behavior under `extensions/` |
| `consumer_tests` | The site after one-time creation | browser, integration, and offline behavior tests |
| `site_policy` | The site | license, citation, contribution, and conduct files |
| `generated` | Ignored runtime output | projection under `generated/` |

If a downstream edit to a template-owned path overlaps an update, Copier writes a `.rej` conflict and the update stops for human review.
Site-specific operating guidance belongs in `site/README.md`, not in this template-owned document.

Copier creates initialized and test paths once, then excludes them from later overwrites.
The updater compares protected site-owned bytes before and after its run.
Generated projection and detailed updater state are ignored; Git records the
reviewable framework and source changes.

`orinoco.lock` is the readable release authority.
Its diff carries exact engine, runtime, template, and workflow changes.
The matching `pixi.toml` wheel URL includes the reviewed SHA-256, and the frozen `pixi.lock` must resolve the same URL and version.
Ownership verification checks those pins together and, when the engine is installed, checks its distribution version.

Semantic content changes are never implicit.
A framework update that genuinely requires one must name a migration and list exact allowed site-owned paths.
The ledger records the changed hashes and remains in `human-review` status.

The site owns every tracked byte below `.orinoco-lite/tests/browser/`, including the npm manifest and lock.
The template owns only the installer facade; it must leave those tracked inputs unchanged.
See the checked browser README for the site-owned acceptance surface.

The template contract is maintained in the [template repository](https://github.com/con/orinoco-lite-template).
Command semantics belong to the [engine repository](https://github.com/con/orinoco-lite-dev).
