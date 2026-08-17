# Consumer browser acceptance

These tests carry the accepted project-path graph and credential-free static
editor scenarios into the flattened consumer. `consumer-contract.json` is the
single seam for Pages, catalog, review-bundle, and editor-apply coordinates.

Run them only after building `build/pages` with the repository's project Pages
base URL. The two definitions run once in Chromium and once in WebKit, for four
active consumer executions.
