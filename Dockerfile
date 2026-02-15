FROM python:3.14.3-bookworm

ARG UID=1002
ARG GID=1002

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY yt-dlp.conf .
COPY src/ ./src/
COPY entrypoint.sh .

COPY test_videos.tx[t] .

RUN mkdir -p /app/data/downloads

RUN groupadd -g ${GID} archiver \
    && useradd -u ${UID} -g ${GID} archiver \
    && chown -R archiver:archiver /app

USER archiver
ENV PATH="/home/archiver/.local/bin:${PATH}"

ENTRYPOINT ["./entrypoint.sh"]
