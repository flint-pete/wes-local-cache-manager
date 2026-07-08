#!/usr/bin/env bash
# test-add-node.sh -- configure a node for /local-cache and launch the
# wes-local-cache-manager DaemonSet as a TEST service.
#
# This does the manual equivalent of what the WES ansible node-setup + kustomize
# stack would eventually do for us in production. Every step below is a candidate
# to fold into ansible later; they are grouped and commented so the migration is
# mechanical. Run ON the node (or via `ssh node-XXXX.sage 'bash -s' < this`).
#
# What it does:
#   1. provision the shared cache host dir (sibling of /media/plugin-data/uploads)
#   2. build the manager image natively with podman (avoids the ECR runc bug)
#   3. side-load it into k3s containerd (podman save | k3s ctr images import)
#   4. apply the ConfigMap + DaemonSet
#   5. verify the pod is Running and print its first logs
#
# Idempotent: safe to re-run (re-imports the image, re-applies the manifest).
set -euo pipefail

# --- config -------------------------------------------------------------------
CACHE_DIR="${CACHE_DIR:-/media/plugin-data/local-cache}"
IMAGE="${IMAGE:-localhost/wes-local-cache-manager:test}"
K3S_IMAGE="docker.io/library/wes-local-cache-manager:test"   # name k3s will see
MANIFEST="${MANIFEST:-kubernetes/test/wes-local-cache-manager.test.yaml}"
KUBECONFIG_ADMIN="/etc/rancher/k3s/k3s.yaml"                 # admin: can touch daemonsets
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "== wes-local-cache-manager test-add =="
echo "   cache dir: $CACHE_DIR"
echo "   image:     $IMAGE"
echo

# === STEP 1: provision the cache host dir  [ANSIBLE CANDIDATE: node fs setup] ==
# Mirrors how /media/plugin-data/uploads is provisioned. World-writable+sticky so
# plugin pods running as various uids can each create their own <ns>/<plugin>
# subtree and read across them (the Layer-1 cross-user-read requirement). The
# sticky bit (1777) stops one plugin deleting another's files by name.
echo "[1/5] provisioning $CACHE_DIR (sudo)"
sudo mkdir -p "$CACHE_DIR"
sudo chmod 1777 "$CACHE_DIR"
echo "      $(sudo ls -ld "$CACHE_DIR")"

# === STEP 2: build the image natively  [ANSIBLE CANDIDATE: image build/publish] =
# Native podman build works where the ECR buildkit builder fails (Infra #2). In
# production this image would instead be built once and published to a registry.
echo "[2/5] podman build -> $IMAGE"
podman build -t "$IMAGE" .

# === STEP 3: side-load into k3s containerd  [ANSIBLE CANDIDATE: image distribution]
# podman/buildah storage != k3s containerd storage; must save|import. The pod's
# imagePullPolicy=IfNotPresent then uses this local image with no registry pull.
echo "[3/5] side-load into k3s containerd"
podman tag "$IMAGE" "$K3S_IMAGE"
podman save "$K3S_IMAGE" | sudo k3s ctr images import -
sudo k3s crictl images | grep -E "wes-local-cache-manager" || {
    echo "ERROR: image not visible to k3s after import" >&2; exit 1; }

# === STEP 4: apply ConfigMap + DaemonSet  [ANSIBLE CANDIDATE: kustomize apply] ==
# Uses the admin kubeconfig because DaemonSets live in a system namespace a
# per-user namespace-scoped kubeconfig cannot touch.
echo "[4/5] apply $MANIFEST"
sudo k3s kubectl --kubeconfig "$KUBECONFIG_ADMIN" apply -f "$MANIFEST"

# === STEP 5: verify ==========================================================
echo "[5/5] waiting for pod to be Running..."
for i in $(seq 1 30); do
    phase=$(sudo k3s kubectl --kubeconfig "$KUBECONFIG_ADMIN" \
              get pods -l app.kubernetes.io/name=wes-local-cache-manager \
              -o jsonpath='{.items[0].status.phase}' 2>/dev/null || true)
    [ "$phase" = "Running" ] && break
    sleep 2
done
echo "      phase: ${phase:-unknown}"
echo "--- first logs ---"
sudo k3s kubectl --kubeconfig "$KUBECONFIG_ADMIN" \
    logs -l app.kubernetes.io/name=wes-local-cache-manager --tail=20 || true

echo
echo "== done. teardown with: ./test-remove-node.sh =="
