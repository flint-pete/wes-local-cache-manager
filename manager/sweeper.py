#!/usr/bin/env python3
"""wes-local-cache-manager -- Layer-2 quota backstop for the shared /local-cache.

This service is NOT an uploader and NOT a policy engine. It does exactly one
thing: keep the shared node cache from overrunning the disk, by enforcing two
hard byte caps on a periodic sweep:

  1. per-cache-unit cap  -- each cache "unit" (a subdir CACHE_UNIT_DEPTH levels
     below the root, e.g. <namespace>/<plugin>) may not exceed PER_SUBDIR_MAX_BYTES.
  2. per-node total cap  -- everything under the root may not exceed
     PER_NODE_MAX_BYTES (this pass also catches stray files that sit outside any
     unit, so nothing escapes the ceiling).

DIVISION OF RESPONSIBILITY (the two-layer model -- see local-cache-design.md):
  * Layer 1 (the PLUGIN, via pywaggle2 / its own ring): graceful, semantics-aware
    eviction -- WHICH files are still needed (newest-N, LRU rows, per-camera...).
    Only the plugin knows its data's meaning, so only the plugin can do this.
  * Layer 2 (THIS service): a blunt, semantics-free safety net. It evicts
    OLDEST-FIRST by mtime and only ever fires against a unit that has ALREADY
    blown past its allocation -- i.e. a misbehaving plugin. A well-behaved plugin
    stays far under its cap and is never touched here.

This service does NOT upload anything and does NOT decide when files are "done"
(that is the upload-agent's and the plugin's job, respectively). Like the
upload-agent it emits status to stdout (kubectl logs) each sweep.

WHY A BESPOKE SWEEP INSTEAD OF A KUBERNETES QUOTA: /local-cache is a hostPath
shared across pods. kubelet's ephemeral-storage accounting does NOT track
hostPath bytes, and emptyDir sizeLimit does not apply. A periodic filesystem
sweep is the only portable mechanism that can bound it. (Stronger hardening --
XFS/ext4 project quotas -- is filesystem-dependent; see README.)
"""

import logging
import os
import time

log = logging.getLogger("cache-manager")


def _env_int(name, default):
    v = os.environ.get(name)
    return int(v) if v else default


def _env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# --- configuration (via the wes-local-cache-manager-env ConfigMap) ------------
CACHE_ROOT       = os.environ.get("CACHE_ROOT", "/local-cache")
SWEEP_INTERVAL   = _env_int("SWEEP_INTERVAL_SECONDS", 60)
PER_SUBDIR_MAX   = _env_int("PER_SUBDIR_MAX_BYTES", 2 * 1024 ** 3)   # 2 GiB
PER_NODE_MAX     = _env_int("PER_NODE_MAX_BYTES", 15 * 1024 ** 3)    # 15 GiB
CACHE_UNIT_DEPTH = _env_int("CACHE_UNIT_DEPTH", 2)                   # <ns>/<plugin>
DRY_RUN          = _env_bool("DRY_RUN")                              # log, don't delete
HEALTH_FILE      = os.environ.get("HEALTH_FILE", "/tmp/healthy")


def cache_units(root, depth):
    """Yield directories exactly `depth` levels below root -- the cache units.
    A unit groups one owner's files, e.g. /local-cache/<ns>/<plugin>. We do not
    descend past a unit (its whole subtree counts toward that unit)."""
    root = root.rstrip("/")
    base = root.count(os.sep)
    for dirpath, dirnames, _ in os.walk(root):
        level = dirpath.count(os.sep) - base
        if level >= depth:
            if level == depth:
                yield dirpath
            dirnames[:] = []  # stop descending at (or below) unit depth


def files_by_age(path):
    """Regular files under `path` as (mtime, size, fullpath), OLDEST FIRST.
    Files that vanish mid-scan (racing a plugin or a prior eviction) are skipped."""
    out = []
    for dirpath, _, names in os.walk(path):
        for name in names:
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp)
            except (FileNotFoundError, OSError):
                continue
            out.append((st.st_mtime, st.st_size, fp))
    out.sort(key=lambda t: t[0])
    return out


def per_unit_cap(unit_path):
    """Byte cap for one cache unit.

    TODO(extension -- plugin size requests): a plugin asking for a larger cache
    allocation would be honored HERE. Options, cheapest first:
      * read a `.cache-quota` sidecar file that SES writes into the unit from a
        `sage.yaml` field (e.g. `local_cache_max: 8Gi`);
      * look up a pod/namespace annotation via the k8s API;
      * consult a manager ConfigMap keyed by <namespace>/<plugin>.
    Whatever the source, the per-node hard cap (PER_NODE_MAX) ALWAYS wins, so a
    single unit can never starve the node. For v1 every unit gets PER_SUBDIR_MAX.
    """
    return PER_SUBDIR_MAX


def evict(files, bytes_to_free):
    """Delete oldest-first from `files` until >= bytes_to_free is reclaimed.
    Returns (freed_bytes, deleted_count). Honors DRY_RUN (logs, deletes nothing)."""
    freed = deleted = 0
    for _mtime, size, fp in files:
        if freed >= bytes_to_free:
            break
        if DRY_RUN:
            log.info("would evict %s (%d bytes)", fp, size)
        else:
            try:
                os.remove(fp)
            except FileNotFoundError:
                continue
            except OSError as e:
                log.warning("could not evict %s: %s", fp, e)
                continue
            log.info("evicted %s (%d bytes)", fp, size)
        freed += size
        deleted += 1
    return freed, deleted


def sweep():
    """One enforcement pass: per-unit caps, then the node-wide backstop."""
    units = list(cache_units(CACHE_ROOT, CACHE_UNIT_DEPTH))

    # pass 1 -- per-unit caps (isolation: one greedy plugin starves only itself)
    for unit in units:
        files = files_by_age(unit)
        size = sum(f[1] for f in files)
        cap = per_unit_cap(unit)
        if size > cap:
            freed, n = evict(files, size - cap)
            log.info("unit over cap: %s  %d > %d  -> evicted %d files (%d bytes)",
                     unit, size, cap, n, freed)

    # pass 2 -- node-wide backstop (also mops up stray files outside any unit)
    allfiles = files_by_age(CACHE_ROOT)
    total = sum(f[1] for f in allfiles)
    if total > PER_NODE_MAX:
        freed, n = evict(allfiles, total - PER_NODE_MAX)
        log.info("node over cap: %d > %d  -> evicted %d files (%d bytes)",
                 total, PER_NODE_MAX, n, freed)
        total -= freed

    log.info("sweep ok: %d units, node_total=%d bytes (node cap %d, per-unit cap %d)%s",
             len(units), total, PER_NODE_MAX, PER_SUBDIR_MAX,
             " [DRY_RUN]" if DRY_RUN else "")


def _mark_healthy():
    """Touch the health file each successful sweep. The liveness probe removes it;
    if a stuck loop fails to recreate it in time, the probe's rm fails and k8s
    restarts us (same pattern as wes-upload-agent)."""
    try:
        open(HEALTH_FILE, "w").close()
    except OSError as e:
        log.warning("could not write health file %s: %s", HEALTH_FILE, e)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("wes-local-cache-manager start: root=%s interval=%ds per_subdir=%d "
             "per_node=%d unit_depth=%d dry_run=%s",
             CACHE_ROOT, SWEEP_INTERVAL, PER_SUBDIR_MAX, PER_NODE_MAX,
             CACHE_UNIT_DEPTH, DRY_RUN)
    while True:
        try:
            sweep()
            _mark_healthy()
        except Exception:
            log.exception("sweep failed")
        if _env_bool("RUN_ONCE"):   # single pass, for tests / manual runs
            return
        time.sleep(SWEEP_INTERVAL)


if __name__ == "__main__":
    main()
