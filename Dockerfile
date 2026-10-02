# wes-local-cache-manager -- Layer-2 quota backstop for the shared /local-cache.
#
# Pure-stdlib Python; no third-party deps, no pip layer. python:3.12-slim avoids
# the stale waggle/plugin-base (Python 3.8). For the test-add this image is built
# natively on the node with podman (see test-add-node.sh).
FROM python:3.12-slim

WORKDIR /app
COPY manager/sweeper.py /app/

# No RUN pip -- stdlib only. If deps are ever added, keep them minimal.
ENTRYPOINT ["python3", "-u", "/app/sweeper.py"]
