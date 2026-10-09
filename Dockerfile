FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Каталог под SQLite создаём в образе и отдаём рабочему пользователю: свежий
# именованный том наследует эту владельцу, иначе сервис не сможет создать файл.
RUN useradd --system --uid 10001 --home /srv probe \
    && mkdir -p /data \
    && chown probe:probe /data
USER probe

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/healthz', timeout=4)"]

CMD ["python", "-m", "app"]
