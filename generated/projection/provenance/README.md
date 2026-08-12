# Historical projection ledger

`milestone-3-SHA256SUMS` is the byte-identical accepted Milestone 3 ledger.
Its `profiles/con` paths are historical evidence only and are never used as
the flattened consumer's active projection digest. The Orinoco Lite engine
generates and verifies the v2 `generated/projection/SHA256SUMS` ledger.
