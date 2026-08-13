# Executable warmed-cache acceptance

Run the gate in two explicit phases from a clean consumer checkout:

```console
python3 tests/offline/run_offline_acceptance.py prepare
python3 tests/offline/run_offline_acceptance.py deny
```

The online `prepare` phase performs a frozen Pixi install, installs and verifies
the locked runtime, runs `pixi run assets-hydrate` and `pixi run assets-verify`
for all sixteen payloads by size and SHA-256 digest, builds the project-path
editor, and constructs a one-record review bundle bound to the current commit,
source path, and source digest. It validates that bundle with the editor's
dry-run command and records a digest of every tracked file under ignored
`build/offline/` state.

The `deny` phase applies and proves the operating-system boundary before
running any accepted offline operation. On macOS the runner invokes
`sandbox-exec` with `tests/offline/macos-network-deny.sb` and requires a socket
operation to fail with an OS policy error. On Linux it uses non-interactive
`sudo unshare --net`, requires a network namespace distinct from the host with
only loopback and no routes, and fails closed when that boundary is
unavailable. A proxy-only simulation is never accepted.

Inside that boundary, the runner performs asset verification, validation,
projection verification, projection update and a second verification, two
independent site builds and their exact digest comparison, and an editor
bundle dry-run. It then proves that the complete tracked file snapshot is
byte-for-byte unchanged and that the Git worktree remains clean.

This proves warmed-cache offline operation. Materializing the sixteen annex
payloads in the repository, and complete cold-offline operation, remain
deferred by the accepted M4-I002 asset policy.
