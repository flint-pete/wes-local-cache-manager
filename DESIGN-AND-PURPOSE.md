# wes-local-cache-manager — Design and Purpose

A small Waggle Edge Stack (WES) service that enables a **shared, node-local cache**
for plugins — a place where one plugin can write data that *another* plugin on the
same node can read. It complements the existing `/uploads` path (which sends data
*off* the node to the cloud) with a `/local-cache` path for data that should stay
*on* the node and be shared between plugins.

This document is for **sysadmins deploying it** and **plugin developers using it**.
It explains what the component does, how it protects the node, and how to stand it
up today (the temporary start/teardown scripts, since it is not yet in Ansible).
You do not need any prior context to adopt it.

---

## 1. The gap it fills: `/uploads` vs `/local-cache`

WES already gives every plugin an outbound spool at `/uploads`
(`/run/waggle/uploads` in the pod). The model there is well understood:

> A plugin drops a file in `/uploads`; the `wes-upload-agent` DaemonSet periodically
> ships it to the cloud (Beehive) and then **deletes** it. `/uploads` is a transient
> outbound queue — write-and-forget, drained upstream.

That covers "get my data to the cloud." It does **not** cover a growing class of
edge workloads:

> **Producer/consumer on the same node.** One plugin produces data (image frames, a
> rolling table, intermediate features) that a *second* plugin on the same node
> consumes (an inference model, an aggregator, a summarizer) — often without ever
> uploading the raw producer output.

`/uploads` cannot serve this because it is transient and pod-private-in-spirit
(files disappear once uploaded), and `/tmp` inside a pod is worse — it is ephemeral
and invisible to any other pod. There has been **no shared, node-persistent place**
for plugins to hand data to each other.

`wes-local-cache-manager` introduces exactly that place — `/local-cache` — and,
critically, the piece that makes it *safe* to have: a bound on how much disk it can
consume.

### The two paths, side by side

| | `/uploads` (existing) | `/local-cache` (this component) |
|---|---|---|
| Purpose | Send data to the cloud | Share data between plugins on the node |
| Lifecycle | Drained + deleted after upload | Retained until evicted by cache policy |
| Managed by | `wes-upload-agent` (uploads, then deletes) | `wes-local-cache-manager` (bounds size only) |
| Backing store | hostPath `/media/plugin-data/uploads` | hostPath `/media/plugin-data/local-cache` |
| Cross-plugin? | No (outbound only) | **Yes** — producer writes, consumer reads |

The two are deliberately the *same architectural shape* (a shared hostPath under
`/media/plugin-data`, managed by a small DaemonSet). `wes-local-cache-manager` is,
by design, a sibling of `wes-upload-agent` — if you understand one, you understand
the other.

---

## 2. What the component actually does

A shared cache that anyone can write to needs a guardrail: without one, a single
buggy or busy plugin can fill the disk and take down the node (and every other
plugin on it). `wes-local-cache-manager` is that guardrail and nothing more.

It is a tiny DaemonSet that, on a periodic sweep, enforces **two size caps** by
deleting **oldest-first** — and it only ever deletes from a cache area that has
*already exceeded* its allowance:

1. **Per-unit cap** (`PER_SUBDIR_MAX_BYTES`, default **2 GiB**).
   A "cache unit" is a subdirectory a fixed depth below the root — by default
   `<namespace>/<plugin>`. Each unit gets its own cap, so **one greedy plugin
   starves only itself**, never its neighbors.

2. **Per-node cap** (`PER_NODE_MAX_BYTES`, default **15 GiB**).
   The outer ceiling across the entire cache root. This pass also sweeps up stray
   files that don't belong to any unit, so nothing escapes the total bound.

That's the whole job. It does **not** upload anything, and it does **not** decide
*which* files matter — see the two-layer model below.

The defaults (2 GiB per plugin, 15 GiB per node) are conservative and configurable
via a ConfigMap; a sysadmin tunes them per fleet without touching code.

---

## 3. The two-layer model (the important mental model)

Managing a cache splits cleanly into two responsibilities that must live in
different places:

- **Layer 1 — the policy (in the plugin).** *Which* files are still worth keeping is
  a question only the plugin can answer, because only the plugin knows what its data
  *means*: keep the newest N frames, keep the last hour, evict least-recently-used
  rows, keep one image per camera. This graceful, meaning-aware eviction belongs in
  the plugin (typically via a shared library primitive), and it should keep the
  plugin comfortably under its cap during normal operation.

- **Layer 2 — the backstop (this component).** A blunt, semantics-free safety net
  that assumes nothing about the data. It fires *only* when a unit has blown past its
  cap — i.e. a misbehaving or misconfigured plugin — and then reclaims space
  oldest-first. **A well-behaved plugin is never touched by Layer 2.**

The division is the same one `/uploads` already uses: the *plugin* decides what to
produce; the *platform service* handles the lifecycle mechanics. Here the platform
service handles only the disk bound, leaving retention *policy* to the producer.

### Worked example: `image-sampler2` (a producer)

`image-sampler2` runs in continuous mode, capturing frames from a camera and writing
them into `/local-cache/<namespace>/image-sampler2/<camera>/`.

- **Layer 1 (its job):** it maintains a bounded ring — e.g. `--cache-max-count 5`
  keeps only the newest 5 frames, evicting the oldest as new ones arrive. It stays
  well under its 2 GiB unit cap on its own.
- **Layer 2 (this component's job):** on each sweep it *observes* that unit, measures
  its size against the caps, and — because the plugin is behaving — does nothing.

If that plugin had a bug and wrote frames without evicting, its unit would eventually
cross 2 GiB and Layer 2 would start trimming the oldest frames to hold the line —
protecting the node and the other plugins, at the cost of the buggy plugin's excess
data (an acceptable trade for a misbehaving producer).

A downstream consumer plugin (say an inference model) mounts the *same*
`/local-cache` and reads those frames — the producer/consumer handoff this component
exists to enable.

### The model is not image-specific

Nothing about Layer 2 assumes images. The unit is just a directory and the cap is
just bytes, so any producer/consumer shape works:

- **A rolling database / table.** A plugin keeps a SQLite file or Parquet tables in
  its unit, applying its *own* LRU/row-count policy (Layer 1). Layer 2 only ensures
  the DB directory can't grow past its cap and sink the node.
- **Intermediate feature files, tiles, model outputs, sensor spool** — same story.
  As long as a producer keeps its own footprint sane, Layer 2 stays out of the way;
  if it doesn't, Layer 2 keeps the node alive.

The only contract a plugin must honor is: **do your own eviction (Layer 1), and
treat Layer 2 as a hard wall you should never actually hit.**

---

## 4. Why a filesystem sweep instead of a Kubernetes quota

A fair question for anyone reviewing this: why not just use a Kubernetes storage
limit? Because they don't apply here.

`/local-cache` is a **hostPath** shared across pods (that sharing is the entire
point — it's how a consumer sees a producer's files). Kubernetes' `ephemeral-storage`
accounting does **not** track hostPath bytes, and `emptyDir` `sizeLimit` does not
apply to hostPath either. The only node-level guard that exists today is generic
disk-pressure eviction, which is blunt and can evict the wrong pod.

So a periodic filesystem sweep is the *only* portable mechanism that can actually
bound a shared hostPath. (Stronger hardening via XFS/ext4 project quotas is possible
but filesystem-dependent; the sweep works everywhere.)

---

## 5. Deploying it — the temporary start / teardown scripts

The component is **not yet folded into the WES Ansible / kustomize stack**. Until it
is, two scripts do the manual equivalent of what node-setup + kustomize will
eventually do. Each step in the add script is labeled as an "ANSIBLE CANDIDATE" so
the future migration is mechanical.

Run them **on the node** (or pipe over SSH: `ssh node-XXXX.sage 'bash -s' < script`).

### `test-add-node.sh` — provision + deploy

Idempotent; safe to re-run. It performs five steps:

1. **Provision the cache host dir** — creates `/media/plugin-data/local-cache`
   (sibling of the existing `/media/plugin-data/uploads`) as world-writable + sticky
   (`1777`), so plugin pods running under different UIDs can each create their own
   `<namespace>/<plugin>` subtree and read across them, while the sticky bit stops
   one plugin from deleting another's files by name.
   *(ANSIBLE CANDIDATE: node filesystem setup.)*
2. **Build the image** natively with `podman` (works where the ECR build path
   currently fails). In production this image would be built once and published to a
   registry. *(ANSIBLE CANDIDATE: image build/publish.)*
3. **Side-load into k3s containerd** (`podman save | k3s ctr images import`), because
   podman's image store is separate from k3s's. *(ANSIBLE CANDIDATE: image
   distribution.)*
4. **Apply** the ConfigMap + DaemonSet with the admin kubeconfig.
   *(ANSIBLE CANDIDATE: kustomize apply.)*
5. **Verify** the pod reaches `Running` and print its first sweep logs.

```bash
# on the node
cd wes-local-cache-manager
./test-add-node.sh
```

Tunable via env vars: `CACHE_DIR` (default `/media/plugin-data/local-cache`),
`IMAGE`, `MANIFEST`.

### `test-remove-node.sh` — teardown

Removes the DaemonSet + ConfigMap and the side-loaded image. By default it **leaves
the cache directory and its contents intact** (that data may still be wanted); pass
`WIPE_CACHE=1` to also empty it.

```bash
./test-remove-node.sh              # remove service, keep cached data
WIPE_CACHE=1 ./test-remove-node.sh # remove service AND empty the cache
```

### Two deployment caveats worth knowing

- **Node scope.** The prototype manifest pins the DaemonSet to a single test node
  via `nodeSelector`. A real deployment removes that pin and runs on every node
  (exactly like `wes-upload-agent`, whose selector is empty).
- **Image name must match what k3s stores.** The side-load retags the image to
  `docker.io/library/wes-local-cache-manager:test`, so the manifest references that
  name. If the manifest instead referenced `localhost/...`, kubelet would try to
  *pull* it and fail with `ImagePullBackOff`. Keep the manifest image ref aligned
  with the side-loaded name.

---

## 6. Configuration reference

All knobs come from the `wes-local-cache-manager-env` ConfigMap:

| Variable | Default | Meaning |
|---|---|---|
| `CACHE_ROOT` | `/local-cache` | Cache root inside the pod (mount of the host dir). |
| `SWEEP_INTERVAL_SECONDS` | `60` | Seconds between enforcement sweeps. |
| `PER_SUBDIR_MAX_BYTES` | `2147483648` (2 GiB) | Per-unit hard cap. |
| `PER_NODE_MAX_BYTES` | `16106127360` (15 GiB) | Per-node hard cap (outer ceiling). |
| `CACHE_UNIT_DEPTH` | `2` | Directory levels below root that define a "unit" (`<ns>/<plugin>`). |
| `DRY_RUN` | unset | If set (`1`/`true`), log what *would* be evicted without deleting — useful when first enabling it on a fleet. |

The service logs one status line per sweep (visible via `kubectl logs`), reporting
the number of units, the node total, and the active caps — so operators can watch
headroom and confirm the backstop is (correctly) idle under normal load.

---

## 7. What a plugin developer needs to do

To participate in the shared cache, a plugin:

1. **Writes into `/local-cache`** — specifically its own unit,
   `/local-cache/<namespace>/<plugin>/...`. (A shared library helper resolves this
   path; e.g. `image-sampler2` auto-detects `/local-cache` when it is mounted.)
2. **Is started with the volume mounted** into the pod, e.g.:
   ```
   pluginctl run ... -v /media/plugin-data/local-cache/<ns>/<plugin>:/local-cache ...
   ```
3. **Implements its own Layer-1 eviction** (a size/count/age policy suited to its
   data) so it stays under the per-unit cap during normal operation.
4. **Fails cleanly if the cache is required but absent.** A producer that *expects*
   the shared cache should refuse to run (with a clear message) on a node lacking
   this component or the volume mount, rather than silently writing to pod-ephemeral
   `/tmp` where no consumer can see it. (`image-sampler2` does this via its
   `--require-local-cache` flag.)

A **consumer** plugin does the mirror image: mount the same `/local-cache` and read
the producer's unit.

---

## 8. Future enhancements (explicitly out of scope for v1)

The v1 goal is small, safe adoption. A few natural extensions are intentionally left
for later:

- **No discovery mechanism.** There is currently no registry that tells a consumer
  *which* producers are writing *what* into `/local-cache`, or where. Producers and
  consumers agree on paths by convention (`<namespace>/<plugin>/...`). A future
  discovery/advertisement mechanism (a manifest of active caches, or a well-known
  index) would let consumers find producers dynamically instead of by prior
  agreement.
- **Per-plugin size requests.** Every unit gets the same `PER_SUBDIR_MAX_BYTES`
  today. A plugin that legitimately needs a bigger allocation could request one via a
  `sage.yaml` field surfaced to the manager (e.g. a `.cache-quota` sidecar SES
  writes, a pod annotation, or a manager ConfigMap keyed by `<ns>/<plugin>`). The
  per-node cap would always win, so no single plugin could ever starve the node.
- **Filesystem project quotas** as optional hardening where the backing filesystem
  supports them (XFS/ext4), turning the soft sweep into a hard `ENOSPC` wall.
- **Ansible/kustomize integration** to replace the temporary scripts in §5, folding
  provisioning + deployment into the standard WES node setup alongside
  `wes-upload-agent`.

None of these block adoption: the component is useful and safe as-is, and each
enhancement is additive.

---

## 9. Summary

`wes-local-cache-manager` adds a **shared, node-local cache** (`/local-cache`) that
lets plugins hand data to each other on the same node — the producer/consumer model
that `/uploads` (cloud-bound, transient) never supported. It makes that cache safe
with **two size caps** (per-plugin and per-node) enforced by a small DaemonSet that
mirrors `wes-upload-agent`. Retention *policy* stays with the plugin (Layer 1); this
service is only the disk *backstop* (Layer 2). Stand it up today with the two
temporary scripts; adoption is small and requires no understanding beyond this
document.
