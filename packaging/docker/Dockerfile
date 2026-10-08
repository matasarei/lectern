# Lectern in Docker: the option beside the host install (packaging/linux/install.sh).
# Build from the repository root, after packaging/build_wheel.sh:
#   docker build -f packaging/docker/Dockerfile -t lectern .
# Run it with packaging/docker/compose.yaml (the GPU, the data volume, the host network).
FROM python:3.12-slim

# uv, pinned (packaging/pins.sh) by tag and by digest (the index of its images), to install
# the wheel.
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv

RUN useradd --system --home-dir /data --create-home --shell /usr/sbin/nologin lectern

COPY dist/lectern_ai-*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/lectern_ai-*.whl && rm /tmp/lectern_ai-*.whl

# Models, projects and the token live in the /data volume. Lectern listens on 127.0.0.1:
# with the host's network (compose.yaml), that's the host's; what forwards to it is up to you.
ENV LECTERN_HOME=/data \
    LECTERN_PORT=8765
USER lectern
VOLUME ["/data"]
# Lectern stops its model runners on SIGINT (as on Ctrl+C).
STOPSIGNAL SIGINT
CMD ["sh", "-c", "exec lectern serve --no-browser --port \"$LECTERN_PORT\""]
