FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    LOG_DIR=/var/log/sn-monitor \
    DIAGNOSTIC_DIR=/var/log/sn-monitor/diagnostics \
    HEALTH_FILE=/run/sn-monitor/health.json \
    TZ=America/Mexico_City

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY monitor.py discord_notifier.py healthcheck.py ./

RUN useradd --create-home --uid 10001 monitor \
    && mkdir -p /var/log/sn-monitor/diagnostics /run/sn-monitor \
    && chown -R monitor:monitor /app /var/log/sn-monitor /run/sn-monitor

USER monitor

CMD ["python", "monitor.py"]
