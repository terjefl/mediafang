#!/bin/sh
set -e

# NRK changes its player API now and then; a fresh yt-dlp on every start keeps the extractor current.
if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
    pip install --no-cache-dir --quiet --upgrade "yt-dlp[default]" || echo "yt-dlp update failed, using bundled version"
fi

exec nrktv --config "${CONFIG:-/config/config.yaml}" "$@"
