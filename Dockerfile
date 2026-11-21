FROM python:3.12-slim

# Install Node.js (for Stremio Cloud Torrent Engine v4.22.3) + FFmpeg (for iPhone/Android live HLS transcoding)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user 1000 and ensure full write ownership of /app
RUN useradd -m -u 1000 user && mkdir -p /app && chown -R user:user /app
WORKDIR /app

COPY --chown=user:user . /app

USER user
ENV HOME=/home/user \
    PORT=8080 \
    DODO_HOST=0.0.0.0 \
    DODO_PRIVACY_MODE=always

# Pre-warm Stremio Web v5.2.0 UI (.gz pre-compressed) + Official Stremio Torrent Server v4.22.3 at build time!
RUN python3 -c "import os, urllib.request, dodo_server; \
dodo_server.ensure_stremio_web_ui(); \
dodo_server._prewarm_heavy_bundles(); \
os.makedirs('/app/.cache/stremio_server', exist_ok=True); \
urllib.request.urlretrieve('https://dl.strem.io/server/v4.22.3/desktop/server.js', '/app/.cache/stremio_server/server.js')"

EXPOSE 8080

CMD ["python3", "dodo_server.py"]
