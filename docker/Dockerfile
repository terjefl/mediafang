FROM python:3.13-slim-bookworm

RUN apt-get update && apt-get -y install --no-install-recommends ffmpeg tzdata fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

# yt-dlp solves YouTube's JavaScript challenges with Deno
COPY --from=denoland/deno:bin-2.5.2 /deno /usr/local/bin/deno

WORKDIR /app

# Single-purpose container: no system Python to protect, so silence pip's root-user warning.
ENV PIP_ROOT_USER_ACTION=ignore

COPY pyproject.toml README.md ./
COPY mediafang mediafang

RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh .
RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
CMD ["loop"]
