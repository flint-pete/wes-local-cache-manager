# Handoff: wes-local-cache-manager → Sage CI team

A proposal to add `wes-local-cache-manager` to the WES rotation. It introduces a
shared, node-local plugin cache (`/local-cache`) — the producer/consumer companion
to `/uploads` — bounded by a small DaemonSet so it can't overrun a node's disk.

Read `DESIGN-AND-PURPOSE.md` for the full rationale; this file is the review
checklist: what's done, and what we're asking the CI team to own.

## What it is (one paragraph)

A tiny stdlib-Python DaemonSet, modeled directly on `wes-upload-agent`. It mounts
the shared hostPath `/media/plugin-data/local-cache` and, on a periodic sweep,
enforces two byte caps by deleting oldest-first — a **per-unit** cap (each directory
`CACHE_UNIT_DEPTH` levels below the root, default 2; default 2 GiB) and a **per-node**
cap (default 15 GiB). It never uploads and never decides *which* files matter;
graceful, semantics-aware eviction stays in the plugin (Layer 1). This service is only
the disk backstop (Layer 2). A well-behaved plugin is never touched.

## Ready for review

- **Verified live on a node (H00F/Thor):** deployed as a DaemonSet, healthy, sweeps
  at production caps; eviction confirmed end-to-end against real camera frames
  written by `image-sampler2` (per-unit eviction fired, neighbor untouched, node
  backstop measured).
- **Unit tests:** `make test` (pure stdlib + pytest) — 21 tests covering oldest-first
  eviction, per-unit isolation, node backstop + stray sweep, DRY_RUN (deletes
  nothing), files vanishing mid-scan, unit-depth boundary, empty-cache safety,
  symlink hardening (world-writable dir), and fail-fast config validation.
- **Two manifests:** `kubernetes/wes-local-cache-manager.yaml` (production: no node
  pin, registry image) and `kubernetes/test/…test.yaml` (single-node side-load
  overlay used by `test-add-node.sh`).
- **Safe rollout built in:** `DRY_RUN=1` logs evictions without deleting —
  recommended for the first fleet deployment.
- **Resource-light:** 32 Mi memory limit, 100 m cpu request; `system-node-critical`
  with a disk-pressure toleration, matching `wes-upload-agent`.

## What we're asking the CI team to own

These are legitimately platform-side and need CI decisions/infra we don't control:

1. **Publish the image.** Build `waggle/wes-local-cache-manager:<tag>` and push to
   the registry the WES stack pulls from. Today it's built natively with `podman`
   on the node and side-loaded (the ECR builder's `runc /proc/acpi` bug blocks the
   normal path — same issue tracked elsewhere). The Dockerfile is stdlib-only, no
   pip layer. Then pin the production manifest to the released tag.
2. **Node provisioning.** Create `/media/plugin-data/local-cache` (sibling of
   `…/uploads`) as world-writable + sticky (`1777`) in the node setup /ansible, so
   plugin pods of differing UIDs can each own a subtree and read across them.
   `test-add-node.sh` does this manually today (step 1, flagged as an ansible
   candidate).
3. **Fold into the WES stack.** Add the production manifest to the kustomize stack
   alongside `wes-upload-agent` (configs + kustomization + node manifest) so it
   deploys fleet-wide, then retire the temporary `test-add-node.sh` /
   `test-remove-node.sh` scripts.
4. **Confirm cross-user reads (the one behavior needing a second plugin).** The
   producer/consumer premise assumes a consumer pod (a different UID) can read a
   producer's files under the sticky shared dir. We provisioned `1777` and verified
   a producer writes, but have not yet run a *separate* consumer pod reading across
   units. Worth confirming with any second plugin during integration.

## Suggested rollout order

1. Publish the image; pin the production manifest to it.
2. Deploy to a canary node with `DRY_RUN=1`; watch the sweep logs for a day.
3. Remove `DRY_RUN`; confirm real eviction only touches over-cap units.
4. Roll out fleet-wide via the kustomize stack.

## Open design question (non-blocking)

Per-plugin size requests: every unit gets the same `PER_SUBDIR_MAX_BYTES` today.
`per_unit_cap()` has a documented extension point (a `sage.yaml` field surfaced to
the manager) if some plugin legitimately needs more. The per-node cap always wins,
so no single plugin can starve the node. Not needed for v1.
