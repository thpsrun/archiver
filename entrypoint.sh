#!/bin/sh
set -e

echo "Upgrading yt-dlp..."
pip install --user --upgrade yt-dlp 2>&1 | tail -1

exec python -m src.main
