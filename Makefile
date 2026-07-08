# wes-local-cache-manager -- build + test.
IMAGE?=waggle/wes-local-cache-manager
RELEASE?=0.0.0

VENV?=.venv
PY=$(VENV)/bin/python

# Unit suite (pure stdlib sweeper; pytest is the only test dep). Self-bootstraps a
# local venv so `make test` just works on any machine with python3 -- no global
# pytest required.
test: $(VENV)
	$(PY) -m pytest -q

$(VENV):
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q pytest

# Native build (podman on the node avoids the ECR runc bug; see test-add-node.sh).
image:
	podman build -t "$(IMAGE):$(RELEASE)" .

clean:
	rm -rf $(VENV) .pytest_cache tests/__pycache__ manager/__pycache__

.PHONY: test image clean
