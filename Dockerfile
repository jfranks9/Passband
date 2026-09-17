FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libgomp1: required at runtime by scikit-learn / scipy wheels.
# tzdata: so TZ (below) is honored for the cron schedules.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*

# supercronic: container-friendly cron. Unlike system cron it inherits the
# process environment, so compose-injected FRESHRSS_*/LITELLM_*/RESEND_* vars
# reach each job, and it logs job output to stdout.
ARG SUPERCRONIC_VERSION=v0.2.29
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py server.py docker-entrypoint.sh ./
COPY passband ./passband
COPY config ./config
RUN chmod +x /app/docker-entrypoint.sh

# Schedules + state paths are env-overridable (see example.env / the Komodo stack).
#
# Image defaults only. Anything set explicitly in the deploy environment
# out-ranks these, which is how an existing store keeps its path across an
# image change: pin PASSBAND_DB_PATH in your compose and the default below
# never applies.
ENV GATHER_SCHEDULE="0 * * * *" \
    NEWSLETTER_SCHEDULE="30 6 * * *" \
    PASSBAND_DB_PATH=/data/passband.db \
    PASSBAND_OUT_DIR=/data/out \
    RUN_ON_START=false \
    TZ=UTC

# Non-root; /data is the persistent volume (SQLite state + rendered output).
RUN useradd --create-home --uid 10001 svc \
    && mkdir -p /data \
    && chown -R svc:svc /data /app
USER svc
VOLUME /data

ENTRYPOINT ["/app/docker-entrypoint.sh"]
