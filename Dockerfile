# Deno: YouTube requires a JavaScript runtime for yt-dlp (see the yt-dlp wiki "EJS")
FROM denoland/deno:bin AS deno

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 plugin \
    && mkdir -p /data /app && chown plugin:plugin /data /app

COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
USER plugin
ENV PATH=/home/plugin/.local/bin:$PATH PYTHONUNBUFFERED=1

COPY --chown=plugin:plugin requirements.txt .
# --user: yt-dlp later updates itself (without root) into the same directory
RUN pip install --no-cache-dir --user -r requirements.txt "uvicorn[standard]>=0.30"
COPY --chown=plugin:plugin app.py audio.py meta.py settings.py ytsource.py plugin.json ./

ENV CACHE_DIR=/data/cache CACHE_LIMIT_GB=20
VOLUME /data
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8100/health', timeout=4)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
