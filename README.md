# VeloraSave processing server

This is the companion engine for the VeloraSave website. The deployed website is a real UI with metadata lookup and thumbnail download routes. Video/audio/playlist preparation requires this service; it is not active merely because the site is published.

## Requirements

- Owner-controlled Linux host with Docker Compose, an HTTPS domain/reverse proxy, 2 CPU cores, 2 GB RAM and at least 20 GB free storage.
- Network access to YouTube and its media hosts. Source restrictions and rate limits can still prevent downloads.
- Content and platform permission appropriate to your use. No DRM, private access or paywall bypass is implemented.

## Start

1. Copy `.env.example` to `.env`.
2. Replace the placeholder token with a random secret of at least 32 characters. Generate one locally with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
3. Run `docker compose up --build -d`.
4. Configure an HTTPS reverse proxy to `127.0.0.1:8000`. Bind the API locally, as the compose file does. Keep the bearer token secret, restrict requests and set request timeouts to at least 60 seconds.
5. Check `GET /health` with header `Authorization: Bearer YOUR_TOKEN`.
6. Configure the Site's private runtime variables `DOWNLOADER_API_URL` (HTTPS base address, no trailing endpoint) and `DOWNLOADER_API_TOKEN` (matching token). Never put these in browser code.
7. Refresh the site; it should report a connected server.

FastAPI docs are disabled. One Uvicorn process is required because job state is in memory. Two background jobs can process concurrently. The queue allows at most ten active jobs. Requests and downloads have practical resource limits; for shared public deployments add durable, per-user job authorization, quotas, request throttling and observability before changing audience. This package is designed for an owner-private installation.

## How it works

- POST `/resolve`: `{ "url": "https://www.youtube.com/watch?v=…", "mode": "video" }` inspects actual metadata and heights. Modes: video, audio, thumbnail, playlist.
- POST `/jobs`: `{ "url": "…", "mode": "video", "quality": 1080, "audio": "mp3", "rights": true, "entries": [] }` returns an opaque job ID.
- GET `/jobs/{id}` returns queued/downloading/processing/ready/failed, progress and a message.
- GET `/jobs/{id}/file` streams the completed file as an attachment.
- Playlist mode returns up to the first 500 accessible entries; larger playlists must be split into batches. A ZIP contains successful files plus an unavailable-items report for failures. Every selected ID is checked again against the source playlist.
- Video prefers available MP4 + M4A streams; other codecs may produce MKV. Audio includes MP3 conversion (320 kbps target) or M4A. Choosing higher bitrate does not improve a compressed source.
- Four fragments can download concurrently. Speed is not guaranteed. The requested resolution is a ceiling; lower source quality is never upscaled.
- Each stream is capped at 4 GB, active output at 16 GB, and jobs at two hours. Keep at least 1 GB disk reserve. The merged result can exceed one individual stream's size. Prepared files and terminal records expire after one hour; a restart clears job state and old app-owned job directories.

## Owner-authorized private access

A private video requires a session for an account that already has access. Never upload passwords or cookies through the site. Follow the current yt-dlp official documentation to produce a session file on your own trusted computer. Mount it read-only using the commented compose example and set COOKIE_FILE locally. Restrict file permissions and ensure the container user (UID 10001) can read it. The service copies it into a temporary directory per operation and deletes that copy after use. Keep the Site owner-private when this is enabled: a shared server session must not grant public visitors your account access.

Unlisted videos may work from the URL if the source makes them available. Authentication alone does not grant download rights. Some sources require additional verification, supported clients or source tokens. This code does not invent tokens or circumvent restrictions.

## Maintain

`docker compose build --pull --no-cache && docker compose up -d` updates the downloader and base image. Test on your own small video after each update. Dependencies use version ranges to allow extractor updates; record your tested image digest for reproducible deployments. Node is included for yt-dlp's supported JavaScript runtime. Check disk usage and source rate limits. Do not log tokens or private sessions.

## Verify

1. Unauthenticated health must return 401; correct token returns 200.
2. A non-YouTube URL, bad ID, HTTP link or a URL with embedded credentials must fail.
3. Resolve a small video you are permitted to save. Verify the title and available heights.
4. Prepare 720p; verify the saved file plays with sound.
5. Prepare MP3 and M4A; check they play and match the original duration.
6. Prepare a two-item playlist. Check files and failure report where relevant.
7. Try an unavailable/private video without authorization. It should fail clearly.
8. Verify expired jobs and restarted jobs return 404.
9. Check that a large file/batch fails within limits and that files are cleaned.

Install `httpx` locally to run the tests. The included `test_server.py` checks authentication, URL allowlisting, job validation and completed file lookup with mocked extraction. It does not verify live YouTube extraction or transcoding. Docker/live downloads must be validated in your hosting environment.

## Primary references

- https://github.com/yt-dlp/yt-dlp
- https://github.com/yt-dlp/yt-dlp/wiki/FAQ
- https://www.youtube.com/t/terms
- https://support.google.com/youtube/answer/56100

## Railway deployment

This repository deploys the companion API only; the VeloraSave website already exists separately.

Use the repository root as the service root. Railway should build the included Dockerfile and route the service to port 8000. Configure API_TOKEN as a random private runtime secret of at least 32 characters before the service starts. Set PORT=8000 and OUTPUT_DIR=/data/jobs. No session file is included.

GET /health requires the bearer token and returns 401 without it. Do not set a Railway HTTP healthcheck to this protected route unless your healthcheck supports that authorization header. After deployment, verify it from a trusted server with the token.

The temporary files are stored on container storage and do not need a persistent volume. Free/Trial storage limits are too small for large 4K jobs and playlists; choose hosting resources appropriate to the expected file sizes. Job records are in memory; use one process and one replica. Restarting or replacing the container clears the jobs. Keep the website owner-private, particularly if a private YouTube session is configured later.

Once the API is healthy, set the website's DOWNLOADER_API_URL to the generated HTTPS service domain and DOWNLOADER_API_TOKEN to the matching secret. Do not expose the token in the repository, frontend code or screenshots.
