#!/bin/sh
# Build a supercronic crontab from the env-driven schedules, then run it.
# supercronic inherits this process's environment, so the compose-injected
# FRESHRSS_* / LITELLM_* / RESEND_* vars are available to every job.
set -eu

CRONTAB=/tmp/passband.cron
{
  echo "${GATHER_SCHEDULE} python /app/main.py gather"
  echo "${NEWSLETTER_SCHEDULE} python /app/main.py newsletter"
} > "$CRONTAB"

echo "passband schedule:"
sed 's/^/  /' "$CRONTAB"

# Optional: seed the store immediately so the first digest isn't empty.
if [ "${RUN_ON_START:-false}" = "true" ]; then
  echo "RUN_ON_START=true -> initial gather"
  python /app/main.py gather || echo "initial gather failed (continuing to schedule)"
fi

exec supercronic "$CRONTAB"
