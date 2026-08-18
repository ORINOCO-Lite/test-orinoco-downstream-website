# Orinoco support files

This tracked directory contains the implementation support behind the small root-level downstream interface.

- `tools/` contains helpers invoked by Pixi tasks and workflows.
- `source-adapters/`, when present, contains template-managed generic adapters and their support data.
- `tests/` contains site behavior, source-adapter, and offline checks.
- `provenance/` is reserved for concise operational evidence that cannot be represented by the source commit itself.

Generated projection and update state are ignored under `generated/` and `.orinoco-lite/state/`.
Downloads, installed runtimes, and caches are ignored under `.orinoco/`, `.pixi/`, and `build/`.
User-facing metadata, editorial content, assets, site customization, source adapters, and configuration remain at the repository root.
