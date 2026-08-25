FROM python:3.12-slim

# No texlive here. Output has been markdown since the start, and ~500 MB of
# LaTeX made every rebuild on the OCI ARM box painful for nothing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini curl \
    && rm -rf /var/lib/apt/lists/*

# supercronic: cron that runs as PID-1-friendly foreground process and logs to
# stdout. Replaces `sleep 86400`, which drifted a little every cycle and so
# never actually ran at 07:00. Multi-arch -- the OCI free tier is ARM.
ARG SUPERCRONIC_VERSION=v0.2.29
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sc="supercronic-linux-amd64" ;; \
      arm64) sc="supercronic-linux-arm64" ;; \
      *) echo "unsupported arch $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/${sc}"; \
    chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app/src \
    TZ=Asia/Kolkata

RUN useradd -u 10001 -m jobpipe && chown -R jobpipe /app
USER jobpipe

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "jobpipe.review_api:app", "--host", "0.0.0.0", "--port", "8080"]
