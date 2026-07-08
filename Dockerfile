# wes-local-cache-manager -- Layer-2 quota backstop for the shared /local-cache.
#
# Pure-stdlib Python; no third-party deps, no pip layer. python:3.12-slim avoids
# both the stale waggle/plugin-base (Python 3.8) and the ECR builder's runc
# /proc/acpi RUN failure (Infra #2) -- though this image is built natively on the
# node with podman anyway (see test-add-node.sh), so it never touches the ECR
# builder for the test-add.
FROM python:3.12-slim

WORKDIR /app
COPY manager/sweeper.py /app/

# No RUN pip -- stdlib only. If deps are ever added, keep them minimal.
ENTRYPOINT ["python3", "-u", "/app/sweeper.py"]
