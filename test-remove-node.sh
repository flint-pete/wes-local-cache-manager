#!/usr/bin/env bash
# test-remove-node.sh -- tear down the wes-local-cache-manager test service.
# Removes the DaemonSet + ConfigMap and the side-loaded image. Does NOT delete
# the cache dir or its contents (that data may still be wanted); pass
# WIPE_CACHE=1 to also empty it.
set -euo pipefail

MANIFEST="${MANIFEST:-kubernetes/test/wes-local-cache-manager.test.yaml}"
CACHE_DIR="${CACHE_DIR:-/media/plugin-data/local-cache}"
K3S_IMAGE="docker.io/library/wes-local-cache-manager:test"
KUBECONFIG_ADMIN="/etc/rancher/k3s/k3s.yaml"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "[1/3] delete DaemonSet + ConfigMap"
sudo k3s kubectl --kubeconfig "$KUBECONFIG_ADMIN" delete -f "$MANIFEST" --ignore-not-found

echo "[2/3] remove side-loaded image from k3s"
sudo k3s ctr images rm "$K3S_IMAGE" 2>/dev/null || true

if [ "${WIPE_CACHE:-}" = "1" ]; then
    echo "[3/3] WIPE_CACHE=1 -> emptying $CACHE_DIR"
    sudo find "$CACHE_DIR" -mindepth 1 -delete 2>/dev/null || true
else
    echo "[3/3] leaving cache dir $CACHE_DIR intact (WIPE_CACHE=1 to empty)"
fi
echo "== teardown done =="
