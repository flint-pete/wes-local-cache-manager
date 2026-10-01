# wes-local-cache-manager

`wes-local-cache-manager` adds a **shared, node-local cache** (`/local-cache`) that
lets plugins hand data to each other on the same node — the producer/consumer model
that `/uploads` (cloud-bound, transient) never supported. It makes that cache safe
with **two size caps** (per-unit and per-node) enforced by a small DaemonSet that
mirrors `wes-upload-agent`. Retention *policy* stays with the plugin (Layer 1); this
service is only the disk *backstop* (Layer 2). Stand it up today with the two
temporary scripts; adoption is small and requires no understanding beyond this
document.

**New here? Read [`DESIGN-AND-PURPOSE.md`](DESIGN-AND-PURPOSE.md)** — the
adoption-focused guide for sysadmins and plugin developers (what it is, how it
complements `/uploads`, the two size caps, the Layer-1/Layer-2 model, and how to
deploy it with the temporary start/teardown scripts).

**Evaluating it for CI rotation? Read [`HANDOFF.md`](HANDOFF.md)** — the review
checklist: what's done/verified and what's CI-owned (image publish, node
provisioning, kustomize integration).

## What it is / is NOT

IT IS a blunt, semantics-free safety net. On a periodic sweep it enforces two
hard byte caps, evicting **oldest-first** only against a cache unit that has
already blown past its allocation:
- **per-unit cap** (`PER_SUBDIR_MAX_BYTES`) on each directory `CACHE_UNIT_DEPTH`
  levels below the root (default 2, e.g. `<cache-name>/<camera>`) -- isolation, so
  one greedy producer starves only its own unit;
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
| `RESERVED_STATE_DIRNAME` | `.state` | top-level dir under the root (`/local-cache/.state/`) that is never counted or evicted (consumer seen-stores); `""` disables the carve-out |
| `DRY_RUN` | (unset) | enabled only by `1`/`true`/`yes`/`on` (case-insensitive): log evictions without deleting. Any other value (e.g. `0`) = real eviction |
| `RUN_ONCE` | (unset) | same truthy values: do one sweep and exit (tests / manual runs). Not set in the ConfigMap |
| `HEALTH_FILE` | `/tmp/healthy` | touched after each successful sweep; the liveness probe removes it, so a stuck loop fails the probe. Not set in the ConfigMap |

## Test-add to a node

```bash
# on the node (or: ssh node-H00F.sage 'bash -s' < test-add-node.sh)
cd wes-local-cache-manager
./test-add-node.sh                 # provision, build, side-load, apply, verify
# ... observe: sudo k3s kubectl logs -l app.kubernetes.io/name=wes-local-cache-manager
./test-remove-node.sh              # tear down (WIPE_CACHE=1 to also empty cache)
```

The scripts call `sudo k3s kubectl` (with the k3s admin kubeconfig) directly, so no
`KUBECTL` variable or separate `kubectl` install is needed. The image is built with
**rootless** `podman` (run the script as your normal user, not with `sudo`; it
calls `sudo` itself where needed).

## Who uses this cache

The cache manager does not know or care who writes to `/local-cache`; it only caps
bytes. In the current media stack the directory layout is:

```
/media/plugin-data/local-cache/          (host path; mounted into pods as /local-cache)
├── camera/top/                          <- media-sampler3 (producer) writes frames:
│     <ts>-v2-<VSN>-top.jpg                 /local-cache/<cache-name>/<name>/<ts>-v2-<VSN>-<name>.jpg
├── camera-crops/top-crop-0/             <- sage-yolo2 reads camera/top, writes crops:
│                                           /local-cache/camera-crops/<name>-crop-<detection-index>/
└── .state/                              <- consumers' seen-stores (sage-yolo2, sage-bioclip2);
                                            RESERVED: never counted, never evicted
```

- [media-sampler3](https://github.com/flint-pete/media-sampler3) — producer: writes
  camera frames (with EXIF provenance) into `<cache-name>/<name>/` units.
- [sage-yolo2](https://github.com/flint-pete/sage-yolo2) — test consumer: reads a
  frame unit, publishes `env.count.*`, writes crops into `camera-crops/…` units.
- [sage-bioclip2](https://github.com/flint-pete/sage-bioclip2) — test consumer:
  reads a crop unit, publishes `env.species.*`.

Each `<cache-name>/<name>` directory (depth 2) is one cache unit with its own cap.
`.state/` is the reserved area (`RESERVED_STATE_DIRNAME`) — see
[HANDOFF.md](HANDOFF.md#reserved-consumer-state-area-state).

**Why every launch passes `--selector zone=core`:** plugins get the cache by
mounting the host dir (`pluginctl run ... -v /media/plugin-data/local-cache:/local-cache`),
and the scheduler refuses volume mounts unless the pod has a node selector (see
[HANDOFF.md — How a plugin gets the `/local-cache` mount](HANDOFF.md#how-a-plugin-gets-the-local-cache-mount),
caveat 1). `--selector zone=core` satisfies that on a Thor node.

End-to-end install (all components, in order):
[INSTALLING-MEDIA-SAMPLER3.md](https://github.com/flint-pete/media-sampler3/blob/master/INSTALLING-MEDIA-SAMPLER3.md).
After a node reboot:
[REBOOT-RECOVERY.md](https://github.com/flint-pete/media-sampler3/blob/master/REBOOT-RECOVERY.md).

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
