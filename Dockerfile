# Memory backend.
#
# Two stages so the runtime image carries no build toolchain: mirage-ai pulls
# aioboto3 and friends, several of which build wheels. Compiling in a stage
# that is then discarded keeps the final image smaller and removes compilers
# from anything that reaches the network.

FROM python:3.12-slim-bookworm AS builder

# Python >= 3.12 is a hard floor: mirage-ai requires it.
WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# --user so the whole install is one directory to copy forward.
RUN pip install --no-cache-dir --user -r requirements.txt


FROM python:3.12-slim-bookworm AS runtime

# Non-root. The service writes only to object storage, so it needs nothing on
# the local filesystem — running as root would be privilege it never uses.
RUN useradd --create-home --uid 10001 app

WORKDIR /app
COPY --from=builder /root/.local /home/app/.local
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts

ENV PATH=/home/app/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Default to real object storage. `disk` mode writes to a container
    # filesystem that vanishes on restart, so it must be opted into rather
    # than fallen back to.
    STORAGE_BACKEND=mirage

USER app
EXPOSE 8000

# /healthz needs no credentials and touches no storage, so it reports process
# liveness rather than whether S3 happens to be reachable — a bucket outage
# should not make the orchestrator kill and restart a healthy container.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)" || exit 1

# No --reload: the reloader watches the filesystem and restarts on writes,
# which in a container with a mounted bucket directory means restarting on
# every request.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
