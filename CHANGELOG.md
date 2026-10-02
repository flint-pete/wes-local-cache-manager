# Changelog

All notable changes to `wes-local-cache-manager`. Format loosely follows
Keep a Changelog; this project uses semantic versioning.

## [0.2.1] - 2026-10-01

- Comments and docs no longer describe the Sage ECR builder as broken; the
  cyberinfrastructure team fixed Thor builds. Native podman build stays the test path.

Documentation + production-manifest pin. No code change, so the image stays
`0.2.0` (the manifest comment notes that the image tag can lag `VERSION`).

### Fixed
- Production manifest pinned `waggle/wes-local-cache-manager:0.1.0`, which predates
  the 0.2.0 `.state` carve-out. Now pins `0.2.0`.
- HANDOFF test count (21 -> 24, including the `.state` tests).
- "per-plugin" cap wording -> "per-unit"; `<namespace>/<plugin>` path examples ->
  `<cache-name>/<source>` (e.g. `camera/top`), matching media-sampler3.
- Bare `kubectl` in docs -> `sudo k3s kubectl`.

### Changed (docs)
- Config tables now list `RESERVED_STATE_DIRNAME`, `RUN_ONCE`, `HEALTH_FILE`, and
  state that `DRY_RUN` is enabled only by `1`/`true`/`yes`/`on`.
- HANDOFF: cross-user reads marked verified (H041 cascade, see the media-sampler3
  install guide); nodeSelector caveat linked to the `--selector zone=core` launches.
- README: new "Who uses this cache" section (directory diagram, links to
  media-sampler3 / sage-yolo2 / sage-bioclip2, install guide, reboot runbook) and a
  note that the test scripts use `sudo k3s kubectl` and rootless podman.

## [0.2.0] - 2026-07-13

Reserved consumer-state area — the sweep now protects durable consumer bookkeeping
from eviction. Enables persistent "seen-store" memory for consumer plugins
(sage-yolo2) so one-shot scheduled runs survive pod restarts without re-processing.

### Added
- `RESERVED_STATE_DIRNAME` (default `.state`): a top-level dir under `CACHE_ROOT`
  (`/local-cache/.state/`) that is **never counted toward any cap and never
  evicted**. Both the per-unit pass and the node-wide backstop skip it (via
  `files_by_age` pruning), and `cache_units` never yields it. Set to `""` to disable.
- Three tests: reserved state survives the node backstop even as the oldest file;
  reserved bytes excluded from the total; reserved dir is not treated as a cache unit.
- HANDOFF "Reserved consumer-state area (`.state`)" section documenting the why and
  the `/local-cache/.state/<plugin>/…` convention.

### Why
Consumer plugins need node-persistent memory of what they've already processed. The
only persistent, plugin-writable path is `/local-cache`, but the blunt oldest-first
backstop would evict a rarely-touched seen-store precisely under disk pressure. This
carve-out makes `/local-cache` the single durable home for both cached frames and
tiny consumer state, with no additional mount.

## [0.1.2] - 2026-07-13

Documentation-only pass (no behavior change): harvested the actionable mount
mechanics from the companion `local-cache-design.md` into HANDOFF.

### Added
- HANDOFF "How a plugin gets the `/local-cache` mount" section: the `volume:`
  field already exists in `PluginSpec` (a job can request the hostPath today), its
  two caveats (requires a nodeSelector; unresolved `IsOwnedByRoot` root-ownership
  TODO), and the recommended `sage.yaml` auto-mount opt-in that also sidesteps the
  root-ownership concern.
- HANDOFF "Open items & known limitations": documented that node-cap eviction is
  globally oldest-first, not offender-weighted — so CI knows the current behavior
  (per-unit isolation is unaffected; a future refinement could evict proportionally
  from the biggest over-cap offenders on a node-cap breach).

## [0.1.1] - 2026-07-13

Documentation-only pass for the CI handoff (no behavior change).

### Fixed
- Corrected the cache-unit convention across README/HANDOFF/DESIGN + manifest
  comment: the manager caps whatever sits at `CACHE_UNIT_DEPTH` (default 2) below the
  root and does not enforce `<namespace>/<plugin>` naming. The real producer
  (`media-sampler3`) uses `<cache-name>/<source>` and mounts the cache ROOT; docs now
  describe the unit generically and fix the plugin-dev mount example (root, not a
  per-plugin subdir — the prior example double-nested the path).
- Dropped a stale `--require-local-cache` reference (the flag was removed;
  continuous mode now requires the cache unconditionally and fails fast if absent).
- Corrected the test count (12 → 21) in HANDOFF.

## [0.1.0] - 2026-07-08

First tagged release — proposed for Sage CI rotation review (see `HANDOFF.md`).

### Added
- Layer-2 quota backstop for the shared `/local-cache` node cache: a small
  stdlib-Python DaemonSet, modeled on `wes-upload-agent`, that enforces two byte
  caps by an oldest-first periodic sweep — a per-unit cap (`<namespace>/<plugin>`,
  default 2 GiB) and a per-node cap (default 15 GiB). Never uploads; never decides
  which files matter (that is the plugin's Layer-1 job).
- `DRY_RUN` mode: logs evictions without deleting (recommended for first-fleet
  rollout).
- Liveness via a health file touched each successful sweep (same pattern as
  `wes-upload-agent`).
- Symlink-safe scanning: uses `os.lstat`, skips non-regular files, and does not
  descend symlinked subdirectories — safe on the world-writable (1777) shared dir.
- Startup config validation: refuses to run (exit 2 → CrashLoopBackOff) on a
  non-positive cap/interval/depth, `per_node < per_subdir`, or a non-numeric cap
  value, so a ConfigMap typo cannot become fleet-wide data loss.
- Unit tests (`make test`, self-bootstrapping venv): 21 tests covering eviction,
  isolation, node backstop, DRY_RUN, symlink hardening, config validation, and
  edge cases.
- Two manifests: production (no node pin, pinned registry image) and a single-node
  test overlay (side-loaded image; node pin commented out by default).
- Docs: `DESIGN-AND-PURPOSE.md` (adoption guide) and `HANDOFF.md` (CI review
  checklist). `.dockerignore` keeps the build context minimal.

### Known limitations (documented, non-blocking)
- Eviction orders by `mtime`, which is perturbable; acceptable for a blunt backstop.
- No discovery mechanism; producers/consumers agree on paths by convention.
- Every unit gets the same cap (no per-plugin size requests yet).
- Not yet folded into the WES Ansible/kustomize stack (temporary scripts provided).
