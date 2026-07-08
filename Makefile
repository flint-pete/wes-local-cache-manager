# wes-local-cache-manager -- build + test.
IMAGE?=waggle/wes-local-cache-manager
RELEASE?=0.0.0
PY?=python3

# Unit suite (pure stdlib + pytest; canonical verification command).
test:
	$(PY) -m pytest -q

# Native build (podman on the node avoids the ECR runc bug; see test-add-node.sh).
image:
	podman build -t "$(IMAGE):$(RELEASE)" .

.PHONY: test image
