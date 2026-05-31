#!/bin/sh
set -e

echo "Upgrading yt-dlp + PO token plugin..."
pip install --user --upgrade yt-dlp bgutil-ytdlp-pot-provider 2>&1 | tail -1

exec python -m src.main
