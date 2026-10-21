# nrktv

Follow series and films on NRK TV and keep them in their own Plex and Jellyfin libraries. Pick
what to follow on a web page (nrk.flagan.net); the container checks NRK every hour, downloads new
episodes with yt-dlp and files them with Kodi-style NFO files, artwork and subtitles, so both
servers show the right show, season, episode, title, date and description without matching
against TVDB/TMDB (where most NRK content isn't).

## How it works

- **Metadata** comes from NRK's catalogue API (psapi.nrk.no); **video and subtitles** from
  yt-dlp's NRK extractor, updated on every container start.
- **What to follow** is chosen on the web page and kept in SQLite (`/data/nrktv.db`). Per series:
  - *Alt som finnes*: every season NRK has, and new episodes as they come
  - *Valgte sesonger*: only the chosen seasons (for date series: years)
  - *Bare nye episoder*: only what NRK publishes after you started following
- **Date series** (talk shows like Lindmo, news like Dagsrevyen) have no episode numbers at NRK.
  The season is the year and the episode number is made from the date, like Pinchflat does:
  22 October is `S2026E102201`. They can keep only the last N days; older broadcasts are deleted.
- **Films and single programs** go to a separate movie library.
- Episodes whose rights end soonest are downloaded first. Rights that have ended and Norway-only
  content outside Norway are marked as such and not retried.

## Layout

```
/library/Fleksnes/tvshow.nfo, poster.jpg, fanart.jpg
/library/Fleksnes/Season 01/Fleksnes - S01E01 - Fysiske fordeler.mp4 | .nfo | .nb.srt | .jpg | -thumb.jpg
/library/Dagsrevyen/Season 2026/Dagsrevyen - S2026E102201 - 2026-10-22.mp4
/movies/Kongehusets hemmelige millioner (2024)/Kongehusets hemmelige millioner (2024).mp4 | .nfo | poster.jpg
```

Every sidecar is written before the video, because Plex scans the moment a video appears.

## Plex and Jellyfin

- **Plex**: a TV library "NRK" on `/Media01/NRK` with the *Plex NFO Series* agent, and a movie
  library "NRK Filmer" on `/Media01/NRK Filmer` with the NFO movie agent. Put their section ids
  in `plex.tv_section` / `plex.movie_section`.
- **Jellyfin**: libraries "NRK" (Shows) and "NRK Filmer" (Movies) on the same paths, with the NFO
  metadata reader on and the online metadata downloaders off. Make an API key under
  Dashboard → API Keys.

## Running

```
cp config.template.yaml config.yaml    # fill in tokens and section ids
uv run nrktv --config config.yaml add https://tv.nrk.no/serie/fleksnes --mode seasons --seasons 1
uv run nrktv --config config.yaml run
uv run nrktv --config config.yaml loop  # web page on web_port, plus a run every interval_minutes
```

Other commands: `search <text>`, `list`, `scan` (find episodes without downloading),
`refresh-art`.

In Docker: see `compose.example.yml`. The image is built by GitHub Actions to
`ghcr.io/terjefl/nrktv` on every push to `main`, and weekly for a fresh yt-dlp. The page has no
login of its own: it belongs behind Cloudflare Access.
