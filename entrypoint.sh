#!/bin/sh
set -e

echo "Upgrading yt-dlp (nightly) + PO token plugin..."
pip install --user --upgrade --pre "yt-dlp[default]" bgutil-ytdlp-pot-provider 2>&1 | tail -1

exec python -m src.main
