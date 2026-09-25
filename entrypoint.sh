#!/bin/sh
set -e

echo "Upgrading yt-dlp (nightly) + PO token plugin..."
if ! pip install --user --upgrade --pre "yt-dlp[default]" bgutil-ytdlp-pot-provider \
        > /tmp/pip-upgrade.log 2>&1; then
    echo "WARNING: upgrade failed, continuing with the image's yt-dlp:"
    tail -5 /tmp/pip-upgrade.log
fi
echo "Using yt-dlp $(yt-dlp --version)"

exec python -m src.main
