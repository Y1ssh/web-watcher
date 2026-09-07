# Web Watcher has no third-party dependencies, so there is nothing to install
# and no lockfile to keep current. The image is the standard library plus one
# package directory.
FROM python:3.13-slim

# Unbuffered output matters more than it looks: a hosting platform reads your
# logs from stdout, and a buffered process appears silent for minutes at a time
# even while it is working.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY watcher/ ./watcher/

# The snapshot and the run log must outlive a deploy. Mount a persistent disk
# here; a container's own filesystem is thrown away on every restart, and a
# watcher that loses its snapshot re-baselines and misses the next real change.
RUN mkdir -p /data

# Nothing here needs root.
RUN useradd --create-home --uid 10001 watcher \
    && chown -R watcher:watcher /data /app
USER watcher

VOLUME ["/data"]

# Fail the build rather than the deploy if the package cannot even import.
RUN python -m watcher --help > /dev/null

# Settings arrive as the WATCHER_CONFIG environment variable (the config holds
# no secrets by design) or as a mounted file passed with --config.
CMD ["python", "-m", "watcher", "watch"]
