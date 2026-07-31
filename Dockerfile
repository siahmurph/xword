FROM python:3.11-slim

WORKDIR /app

# xword-dl shells out to itself as a subprocess and its dependency chain
# pulls in packages that need a C toolchain on some platforms; keep it slim
# but make sure certs/toolchain basics are present for pip installs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static/ static/

ENV DB_PATH=/data/crosswords.db
VOLUME ["/data"]

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8099"]
