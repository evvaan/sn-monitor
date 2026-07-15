FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY monitor.py discord_notifier.py ./

RUN mkdir -p /app/data \
    && useradd --create-home --uid 10001 monitor \
    && chown -R monitor:monitor /app

USER monitor

CMD ["python", "monitor.py"]
