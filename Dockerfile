FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Files rsynced from the Mac can arrive owner-only (e.g. app/kb/voz.md had 600 + an ACL);
# the app runs as 'bot', so make the code and KB world-readable inside the image.
RUN chmod -R a+rX /srv/app \
 && useradd --system --no-create-home bot \
 && mkdir -p /srv/data && chown bot /srv/data
USER bot
# /srv/data is a volume (deploy/docker-compose.yml): data/bot.sqlite3 (conversations,
# agent history, quiz progress, Slack threads, usage) survives redeploys.
VOLUME ["/srv/data"]

EXPOSE 8000
# One worker on purpose: state is cached in process memory and written through to one
# SQLite connection (app/db.py); several workers would each hold a different cache.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
