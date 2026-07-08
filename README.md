# wes-local-cache-manager

Layer-2 quota backstop for the shared node cache `/local-cache`. A tiny
DaemonSet, modeled on `wes-upload-agent`, whose ONLY job is to stop the shared
cache from overrunning the disk.

**New here? Read [`DESIGN-AND-PURPOSE.md`](DESIGN-AND-PURPOSE.md)** — the
adoption-focused guide for sysadmins and plugin developers (what it is, how it
complements `/uploads`, the two size caps, the Layer-1/Layer-2 model, and how to
deploy it with the temporary start/teardown scripts).

Full design: `../local-cache-design.md`. This directory is the working prototype
for the test-add to H00F.

## What it is / is NOT

IT IS a blunt, semantics-free safety net. On a periodic sweep it enforces two
hard byte caps, evicting **oldest-first** only against a cache unit that has
already blown past its allocation:
- **per-unit cap** (`PER_SUBDIR_MAX_BYTES`) on each `<namespace>/<plugin>` subdir
  -- isolation, so one greedy plugin starves only itself;
- **per-node cap** (`PER_NODE_MAX_BYTES`) across the whole root -- the outer
  ceiling, which also mops up stray files outside any unit.

IT IS NOT:
- an uploader -- it never ships anything anywhere (that's `wes-upload-agent`);
- a policy engine -- it does NOT decide which files are still needed. That is
  **Layer 1**, owned by the plugin (via pywaggle2 / its own ring): newest-N,
  MB budget, LRU rows, per-camera... Only the plugin knows its data's meaning.

A well-behaved plugin whose own Layer-1 eviction keeps it under its cap is
**never touched** by this service.

## Why a filesystem sweep (not a k8s quota)

`/local-cache` is a hostPath shared across pods. kubelet's ephemeral-storage
accounting does not track hostPath bytes and emptyDir `sizeLimit` doesn't apply,
so no k8s-native mechanism can bound it. A periodic sweep is the only portable
option. (Stronger hardening -- XFS/ext4 project quotas -- is filesystem-dependent;
noted in the design doc as optional belt-and-suspenders.)

## Files

- `manager/sweeper.py` -- the sweep loop. Pure stdlib. Config via env
  (see the ConfigMap). `RUN_ONCE=1` does a single pass (tests); `DRY_RUN=1` logs
  evictions without deleting.
- `Dockerfile` -- `python:3.12-slim`, no pip layer.
- `kubernetes/wes-local-cache-manager.yaml` -- ConfigMap + DaemonSet.
- `test-add-node.sh` / `test-remove-node.sh` -- provision + launch / tear down on
  a node. Every step is annotated as an ANSIBLE CANDIDATE for the eventual
  production node-setup.

## Config (ConfigMap `wes-local-cache-manager-env`)

| key | default | meaning |
|---|---|---|
| `CACHE_ROOT` | `/local-cache` | cache root inside the pod |
| `SWEEP_INTERVAL_SECONDS` | `60` | seconds between sweeps |
| `PER_SUBDIR_MAX_BYTES` | 2 GiB | per cache-unit hard cap |
| `PER_NODE_MAX_BYTES` | 15 GiB | per-node total hard cap |
| `CACHE_UNIT_DEPTH` | `2` | dir levels below root that define a "unit" |
| `DRY_RUN` | (unset) | if set, log evictions without deleting |

## Test-add to a node

```bash
# on the node (or: ssh node-H00F.sage 'bash -s' < test-add-node.sh)
cd wes-local-cache-manager
./test-add-node.sh                 # provision, build, side-load, apply, verify
# ... observe `kubectl logs -l app.kubernetes.io/name=wes-local-cache-manager`
./test-remove-node.sh              # tear down (WIPE_CACHE=1 to also empty cache)
```

## Local test (no node)

```bash
# build a fixture and run one pass in dry-run
CACHE_ROOT=/tmp/lc RUN_ONCE=1 DRY_RUN=1 \
  PER_SUBDIR_MAX_BYTES=$((3*1048576)) PER_NODE_MAX_BYTES=$((100*1048576)) \
  python3 manager/sweeper.py
```

## Future work (marked in code)

- **Plugin size requests** (`sweeper.py::per_unit_cap`): let a plugin request a
  larger allocation via a `sage.yaml` field -> `.cache-quota` sidecar / pod
  annotation / manager ConfigMap. The per-node cap always wins.
- **Production integration**: fold the manifest into the WES kustomize stack and
  the node-setup steps into ansible (the test scripts mark exactly which steps).
