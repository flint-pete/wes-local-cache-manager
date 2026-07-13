#!/usr/bin/env python3
"""Unit tests for the wes-local-cache-manager sweeper.

Pure-stdlib, no third-party deps beyond pytest. Run with `make test` or
`python3 -m pytest -q`. Config lives in module globals (read from env at import),
so tests set caps via monkeypatch on the module rather than the environment.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "manager"))
import sweeper  # noqa: E402


# --- helpers -----------------------------------------------------------------

def _write(path, size, *, mtime=None):
    """Create a file of `size` bytes, optionally back-dating its mtime."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def caps(monkeypatch):
    """Set small, predictable caps and disable DRY_RUN by default."""
    monkeypatch.setattr(sweeper, "PER_SUBDIR_MAX", 3_000)
    monkeypatch.setattr(sweeper, "PER_NODE_MAX", 10_000)
    monkeypatch.setattr(sweeper, "CACHE_UNIT_DEPTH", 2)
    monkeypatch.setattr(sweeper, "DRY_RUN", False)
    return sweeper


# --- cache_units -------------------------------------------------------------

def test_cache_units_at_depth(tmp_path):
    _write(str(tmp_path / "ns" / "plugin" / "a.jpg"), 1)
    _write(str(tmp_path / "ns" / "other" / "b.jpg"), 1)
    units = sorted(sweeper.cache_units(str(tmp_path), 2))
    assert units == sorted([
        str(tmp_path / "ns" / "plugin"),
        str(tmp_path / "ns" / "other"),
    ])


def test_cache_units_does_not_descend_past_depth(tmp_path):
    # deep subtree under a unit still counts as ONE unit, not many
    _write(str(tmp_path / "ns" / "plugin" / "cam" / "deep" / "x.jpg"), 1)
    units = list(sweeper.cache_units(str(tmp_path), 2))
    assert units == [str(tmp_path / "ns" / "plugin")]


# --- files_by_age ------------------------------------------------------------

def test_files_by_age_oldest_first(tmp_path):
    now = time.time()
    _write(str(tmp_path / "new.jpg"), 1, mtime=now)
    _write(str(tmp_path / "old.jpg"), 1, mtime=now - 100)
    _write(str(tmp_path / "mid.jpg"), 1, mtime=now - 50)
    order = [os.path.basename(f[2]) for f in sweeper.files_by_age(str(tmp_path))]
    assert order == ["old.jpg", "mid.jpg", "new.jpg"]


def test_files_by_age_skips_vanished(tmp_path, monkeypatch):
    _write(str(tmp_path / "a.jpg"), 1)
    real_lstat = os.lstat

    def flaky_lstat(p, *a, **k):
        if str(p).endswith("a.jpg"):
            raise FileNotFoundError(p)
        return real_lstat(p, *a, **k)

    monkeypatch.setattr(sweeper.os, "lstat", flaky_lstat)
    assert sweeper.files_by_age(str(tmp_path)) == []


# --- evict -------------------------------------------------------------------

def test_evict_oldest_first_until_freed(tmp_path, caps):
    now = time.time()
    f_old = _write(str(tmp_path / "old.jpg"), 1000, mtime=now - 100)
    f_mid = _write(str(tmp_path / "mid.jpg"), 1000, mtime=now - 50)
    f_new = _write(str(tmp_path / "new.jpg"), 1000, mtime=now)
    files = sweeper.files_by_age(str(tmp_path))
    freed, n = sweeper.evict(files, 1500)   # need to free >=1500 -> 2 oldest
    assert n == 2 and freed == 2000
    assert not os.path.exists(f_old) and not os.path.exists(f_mid)
    assert os.path.exists(f_new)


def test_evict_dry_run_deletes_nothing(tmp_path, caps, monkeypatch):
    monkeypatch.setattr(sweeper, "DRY_RUN", True)
    f = _write(str(tmp_path / "a.jpg"), 1000)
    files = sweeper.files_by_age(str(tmp_path))
    freed, n = sweeper.evict(files, 1000)
    # accounting still reports what WOULD be freed, but the file survives
    assert n == 1 and freed == 1000
    assert os.path.exists(f)


# --- sweep (integration of the passes) ---------------------------------------

def test_sweep_per_unit_cap_evicts_oldest(tmp_path, caps, monkeypatch):
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    now = time.time()
    unit = tmp_path / "beckman" / "image-sampler2"
    # 5 x 1000B = 5000 > 3000 cap -> evict 2 oldest to land at 3000
    for i in range(5):
        _write(str(unit / f"f{i}.jpg"), 1000, mtime=now - (5 - i) * 10)
    sweeper.sweep()
    remaining = sorted(os.listdir(unit))
    assert remaining == ["f2.jpg", "f3.jpg", "f4.jpg"]  # 2 oldest gone


def test_sweep_isolation_under_cap_untouched(tmp_path, caps, monkeypatch):
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    over = tmp_path / "ns" / "greedy"
    under = tmp_path / "ns" / "tidy"
    for i in range(5):
        _write(str(over / f"f{i}.jpg"), 1000)      # 5000 > cap
    _write(str(under / "keep.jpg"), 500)            # under cap
    sweeper.sweep()
    assert os.path.exists(str(under / "keep.jpg"))  # neighbor untouched
    assert len(os.listdir(over)) == 3               # greedy trimmed to cap


def test_sweep_node_backstop_and_strays(tmp_path, caps, monkeypatch):
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    # each unit under its per-unit cap, but node total blows the node cap; a stray
    # file at the root (outside any unit) is swept by the node pass.
    monkeypatch.setattr(sweeper, "PER_SUBDIR_MAX", 10_000)   # units individually ok
    monkeypatch.setattr(sweeper, "PER_NODE_MAX", 3_500)      # node ceiling low
    now = time.time()
    _write(str(tmp_path / "a" / "p" / "u1.jpg"), 2000, mtime=now - 30)
    _write(str(tmp_path / "b" / "p" / "u2.jpg"), 2000, mtime=now - 20)
    _write(str(tmp_path / "stray.jpg"), 2000, mtime=now - 40)  # oldest -> first out
    sweeper.sweep()
    total = sum(f[1] for f in sweeper.files_by_age(str(tmp_path)))
    assert total <= 3_500
    assert not os.path.exists(str(tmp_path / "stray.jpg"))  # oldest evicted first


# --- reserved consumer-state area (never counted, never evicted) --------------

def test_reserved_state_survives_node_backstop(tmp_path, caps, monkeypatch):
    """The .state area is off-limits: it must survive even when the node cap is
    blown and it is the OLDEST thing on disk (prime eviction target otherwise)."""
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    monkeypatch.setattr(sweeper, "RESERVED_STATE_DIRNAME", ".state")
    monkeypatch.setattr(sweeper, "PER_NODE_MAX", 3_000)
    now = time.time()
    seen = _write(str(tmp_path / ".state" / "sage-yolo2" / "seen"), 500,
                  mtime=now - 9999)                      # oldest -> would go first
    _write(str(tmp_path / "ns" / "p" / "a.jpg"), 2000, mtime=now - 20)
    _write(str(tmp_path / "ns" / "p" / "b.jpg"), 2000, mtime=now - 10)
    sweeper.sweep()
    assert os.path.exists(seen)                           # consumer memory preserved


def test_reserved_state_not_counted_toward_total(tmp_path, caps, monkeypatch):
    """Bytes under .state are excluded from the accounting entirely."""
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    monkeypatch.setattr(sweeper, "RESERVED_STATE_DIRNAME", ".state")
    _write(str(tmp_path / ".state" / "seen"), 5000)
    _write(str(tmp_path / "ns" / "p" / "a.jpg"), 1000)
    total = sum(f[1] for f in sweeper.files_by_age(str(tmp_path)))
    assert total == 1000                                  # .state's 5000 not counted


def test_reserved_state_is_not_a_cache_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    monkeypatch.setattr(sweeper, "RESERVED_STATE_DIRNAME", ".state")
    _write(str(tmp_path / ".state" / "sub" / "seen"), 1)  # sits at unit depth
    _write(str(tmp_path / "ns" / "plugin" / "a.jpg"), 1)
    units = list(sweeper.cache_units(str(tmp_path), 2))
    assert units == [str(tmp_path / "ns" / "plugin")]     # .state/sub NOT a unit


def test_sweep_dry_run_keeps_everything(tmp_path, caps, monkeypatch):
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    monkeypatch.setattr(sweeper, "DRY_RUN", True)
    unit = tmp_path / "ns" / "plugin"
    for i in range(5):
        _write(str(unit / f"f{i}.jpg"), 1000)   # way over cap
    sweeper.sweep()
    assert len(os.listdir(unit)) == 5           # DRY_RUN deleted nothing


def test_sweep_empty_root_no_error(tmp_path, caps, monkeypatch):
    monkeypatch.setattr(sweeper, "CACHE_ROOT", str(tmp_path))
    sweeper.sweep()   # must not raise on an empty cache


# --- per_unit_cap ------------------------------------------------------------

def test_per_unit_cap_is_the_default(caps):
    assert sweeper.per_unit_cap("/local-cache/ns/plugin") == sweeper.PER_SUBDIR_MAX


# --- symlink hardening (world-writable 1777 dir) -----------------------------

def test_files_by_age_skips_symlinked_files(tmp_path):
    real = _write(str(tmp_path / "real.jpg"), 100)
    # a symlink pointing OUTSIDE the cache (e.g. at a sensitive file)
    outside = _write(str(tmp_path.parent / "secret.txt"), 999)
    os.symlink(outside, str(tmp_path / "evil"))
    got = [os.path.basename(f[2]) for f in sweeper.files_by_age(str(tmp_path))]
    assert got == ["real.jpg"]              # symlink ignored, target never counted


def test_files_by_age_does_not_descend_symlinked_dirs(tmp_path):
    # a symlinked subdir pointing at a tree outside the cache must not be traversed
    victim = tmp_path.parent / "victim"
    _write(str(victim / "important.dat"), 500)
    _write(str(tmp_path / "real.jpg"), 100)
    os.symlink(str(victim), str(tmp_path / "link-to-victim"))
    files = sweeper.files_by_age(str(tmp_path))
    names = [os.path.basename(f[2]) for f in files]
    assert names == ["real.jpg"]            # never walked into the symlinked dir
    assert os.path.exists(str(victim / "important.dat"))  # untouched


# --- config validation (fail-fast on dangerous misconfig) --------------------

@pytest.mark.parametrize("attr", ["PER_SUBDIR_MAX", "PER_NODE_MAX",
                                  "SWEEP_INTERVAL", "CACHE_UNIT_DEPTH"])
def test_validate_config_rejects_nonpositive(monkeypatch, caps, attr):
    monkeypatch.setattr(sweeper, attr, 0)
    with pytest.raises(sweeper.ConfigError):
        sweeper.validate_config()


def test_validate_config_rejects_node_below_subdir(monkeypatch, caps):
    monkeypatch.setattr(sweeper, "PER_SUBDIR_MAX", 5000)
    monkeypatch.setattr(sweeper, "PER_NODE_MAX", 1000)
    with pytest.raises(sweeper.ConfigError):
        sweeper.validate_config()


def test_validate_config_accepts_sane(caps):
    sweeper.validate_config()   # caps fixture is valid -> no raise


def test_env_int_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("PER_NODE_MAX_BYTES", "2Gi")
    with pytest.raises(sweeper.ConfigError):
        sweeper._env_int("PER_NODE_MAX_BYTES", 0)
