"""Owner-operated, token-protected processing API. Single-process deployment."""
import asyncio
import contextlib
import hmac
import os
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import FileResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL

API_TOKEN = os.environ.get('API_TOKEN', '')
ROOT = Path(os.environ.get('OUTPUT_DIR', '/tmp/velorasave')).resolve()
TTL = 3600
DEADLINE = 7200
MAX_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 16 * 1024**3
POOL = ThreadPoolExecutor(max_workers=2)
JOBS = {}
LOCK = threading.RLock()
RESOLVE_LOCK = asyncio.Semaphore(2)


def normalize_url(raw: str, playlist=False):
    u = urlparse(raw.strip())
    host = (u.hostname or '').lower()
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        raise ValueError('Use a complete HTTPS YouTube link.')
    if host not in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'):
        raise ValueError('Only YouTube links are supported.')
    q = parse_qs(u.query)
    parts = u.path.strip('/').split('/')
    vid = parts[0] if host == 'youtu.be' else q.get('v', [None])[0]
    if not vid and len(parts) > 1 and parts[0] in ('shorts', 'live', 'embed'):
        vid = parts[1]
    pid = q.get('list', [None])[0]
    import re
    if vid and not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
        raise ValueError('Invalid video ID.')
    if pid and not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', pid):
        raise ValueError('Invalid playlist ID.')
    if playlist:
        if not pid:
            raise ValueError('Paste a link containing a playlist ID.')
        return f'https://www.youtube.com/playlist?list={pid}'
    if not vid:
        raise ValueError('Paste a video link, or select playlist mode.')
    return f'https://www.youtube.com/watch?v={vid}'


def public_error(error):
    text = str(error).lower()
    if 'private' in text or 'sign in' in text or 'login' in text:
        return 'This video needs authorized access or a valid owner session. It may also require source verification.'
    if 'not available' in text or 'unavailable' in text or 'removed' in text:
        return 'This content is unavailable, removed or restricted.'
    if 'format' in text:
        return 'The requested format is unavailable. Inspect the source again or choose a different quality.'
    if 'limit' in text or 'storage' in text or 'deadline' in text:
        return 'This job exceeded a processing or storage limit. Try a smaller file or playlist.'
    return 'The source could not be processed. Retry later or check the server and downloader version.'


@contextlib.contextmanager
def downloader_options(**overrides):
    # Work on a disposable copy: yt-dlp may update the session file on close.
    with tempfile.TemporaryDirectory(prefix='velora-session-') as tmp:
        opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
                'socket_timeout': 20, 'retries': 3, 'fragment_retries': 3,
                'concurrent_fragment_downloads': 4, 'max_filesize': MAX_BYTES,
                'js_runtimes': {'node': {}}, 'cachedir': False}
        source = os.environ.get('COOKIE_FILE')
        if source:
            session = Path(tmp) / 'session.txt'
            shutil.copyfile(source, session)
            opts['cookiefile'] = str(session)
        opts.update(overrides)
        yield opts


def resolve_media(url, mode):
    canonical = normalize_url(url, mode == 'playlist')
    overrides = {'extract_flat': 'in_playlist', 'playlistend': 500, 'noplaylist': False} if mode == 'playlist' else {}
    with downloader_options(**overrides) as opts, YoutubeDL(opts) as ydl:
        info = ydl.extract_info(canonical, download=False)
    if not info:
        raise ValueError('Unavailable source.')
    if mode == 'playlist':
        entries = []
        for e in info.get('entries') or []:
            if not e or e.get('availability') in ('private', 'needs_auth'):
                # Private entries may be usable with the owner's configured session.
                if not os.environ.get('COOKIE_FILE'):
                    continue
            if not e or not e.get('id'):
                continue
            try:
                entry_url = normalize_url(f"https://www.youtube.com/watch?v={e['id']}")
            except ValueError:
                continue
            entries.append({'id': e['id'], 'title': e.get('title') or 'Untitled video', 'url': entry_url})
        if not entries:
            raise ValueError('No accessible entries.')
        return {'title': info.get('title') or 'YouTube playlist', 'channel': info.get('uploader') or 'YouTube',
                'url': canonical, 'entries': entries, 'formats': [], 'connected': True, 'batch_limit': 500}
    if info.get('is_live'):
        raise ValueError('Active live streams cannot be prepared. Use a completed replay.')
    heights = sorted({int(f['height']) for f in info.get('formats', [])
                      if f.get('height') and 0 < f['height'] <= 2160 and f.get('vcodec') not in ('none', None)}, reverse=True)
    return {'id': info['id'], 'title': info.get('title') or 'YouTube video',
            'channel': info.get('uploader') or 'YouTube', 'url': canonical,
            'thumbnail': info.get('thumbnail'), 'formats': [{'height': h, 'ext': 'mp4'} for h in heights], 'connected': True}


def cleanup():
    with LOCK:
        expired = [k for k, j in JOBS.items() if j['status'] in ('ready', 'failed') and time.time() - j['updated'] > TTL]
        for key in expired:
            shutil.rmtree(ROOT / key, ignore_errors=True)
            del JOBS[key]


def mutate(job_id, **values):
    with LOCK:
        if 'progress' in values:
            values['progress'] = max(values['progress'], JOBS[job_id]['progress'])
        JOBS[job_id].update(values, updated=time.time())


def guard_job(job_id):
    with LOCK:
        if time.time() - JOBS[job_id]['created'] > DEADLINE:
            raise RuntimeError('Processing deadline exceeded.')
    total = sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())
    if total > MAX_TOTAL_BYTES or shutil.disk_usage(ROOT).free < 1024**3:
        raise RuntimeError('Storage limit exceeded.')


def download_one(job_id, url, request, folder, index, count):
    def progress(d):
        guard_job(job_id)
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        done = d.get('downloaded_bytes') or 0
        if done > MAX_BYTES:
            raise RuntimeError('File size limit exceeded.')
        pct = min(done / total, .98) if total else 0
        mutate(job_id, status='downloading', progress=round((index + pct) / count * 88, 1),
               message=f'Downloading item {index + 1} of {count}.')
    def postprocess(d):
        guard_job(job_id)
        mutate(job_id, status='processing', message=f'Processing item {index + 1} of {count}.')
    opts = {'outtmpl': str(folder / '%(title).100B [%(id)s].%(ext)s'),
            'restrictfilenames': True, 'progress_hooks': [progress], 'postprocessor_hooks': [postprocess],
            'match_filter': lambda info, **_: 'Active live streams are not supported.' if info.get('is_live') else None}
    if request.mode == 'audio':
        if request.audio == 'mp3':
            opts.update(format='bestaudio/best', postprocessors=[{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}])
        else:
            opts.update(format='bestaudio[ext=m4a]/bestaudio/best', postprocessors=[{'key': 'FFmpegExtractAudio', 'preferredcodec': 'm4a', 'preferredquality': '192'}])
    else:
        h = request.quality
        opts.update(format=f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]/bestvideo[height<={h}]+bestaudio/best[height<={h}]', merge_output_format='mp4/mkv')
    with downloader_options(**opts) as options, YoutubeDL(options) as ydl:
        ydl.extract_info(normalize_url(url), download=True)
    guard_job(job_id)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix in ('.mp4', '.mkv', '.webm', '.m4a', '.mp3')]
    if not files:
        raise RuntimeError('No completed media file.')
    return max(files, key=lambda p: p.stat().st_mtime)


def run_job(job_id, request):
    folder = ROOT / job_id
    try:
        folder.mkdir(parents=True)
        guard_job(job_id)
        if request.mode == 'playlist':
            result = resolve_media(request.url, 'playlist')
            allowed = {e['id']: e for e in result['entries']}
            if not request.entries or any(i not in allowed for i in request.entries):
                raise ValueError('Selected items are no longer available.')
            urls = [allowed[i]['url'] for i in request.entries]
        else:
            urls = [normalize_url(request.url)]
        files = []
        failures = []
        for i, url in enumerate(urls):
            sub = folder / f'item-{i + 1}'
            sub.mkdir()
            try:
                files.append(download_one(job_id, url, request, sub, i, len(urls)))
            except Exception as e:
                if request.mode != 'playlist':
                    raise
                failures.append(f'Item {i + 1}: {public_error(e)}')
        if not files:
            raise ValueError('No items could be processed.')
        mutate(job_id, status='processing', progress=94, message='Finalizing your file…')
        if request.mode == 'playlist':
            final = folder / 'velorasave-playlist.zip'
            with zipfile.ZipFile(final, 'w', compression=zipfile.ZIP_STORED) as archive:
                for i, p in enumerate(files):
                    guard_job(job_id)
                    archive.write(p, arcname=f'{i + 1:02d}-{p.name}')
                if failures:
                    archive.writestr('unavailable-items.txt', '\n'.join(failures))
            for d in folder.glob('item-*'):
                shutil.rmtree(d)
        else:
            final = files[0]
        mutate(job_id, status='ready', progress=100, file=str(final),
               message=f'Ready for one hour. {len(failures)} unavailable item(s) listed inside the ZIP.' if failures else 'Ready to save. This file expires in one hour.')
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        mutate(job_id, status='failed', error=public_error(e), message='Try again with a smaller batch or a different format.')


async def sweeper():
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(cleanup)


@asynccontextmanager
async def lifespan(app):
    if len(API_TOKEN) < 32 or API_TOKEN.startswith('REPLACE_'):
        raise RuntimeError('Set API_TOKEN to a secret with at least 32 characters.')
    ROOT.mkdir(parents=True, exist_ok=True)
    for p in ROOT.iterdir():
        # Only app-owned job directories are cleaned; never arbitrary user data.
        if p.is_dir() and len(p.name) == 32 and all(c in '0123456789abcdef' for c in p.name):
            shutil.rmtree(p, ignore_errors=True)
    task = asyncio.create_task(sweeper())
    yield
    task.cancel()
    POOL.shutdown(wait=False, cancel_futures=True)


async def authorize(authorization: str = Header(default='')):
    expected = f'Bearer {API_TOKEN}'
    if not API_TOKEN or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, 'Unauthorized.')


app = FastAPI(title='VeloraSave processing API', lifespan=lifespan, dependencies=[Depends(authorize)], docs_url=None, redoc_url=None, openapi_url=None)


class ResolveRequest(BaseModel):
    url: str = Field(max_length=2048)
    mode: str = 'video'


class JobRequest(ResolveRequest):
    quality: int = 1080
    audio: str = 'mp3'
    rights: bool = False
    entries: list[str] = Field(default_factory=list, max_length=500)


@app.get('/health')
def health():
    return {'ok': True, 'version': '1.0.0', 'private_session_enabled': bool(os.environ.get('COOKIE_FILE'))}


@app.post('/resolve')
async def resolve(request: ResolveRequest):
    if request.mode not in ('video', 'audio', 'thumbnail', 'playlist'):
        raise HTTPException(400, 'Choose a supported mode.')
    try:
        normalize_url(request.url, request.mode == 'playlist')
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        async with RESOLVE_LOCK:
            return await asyncio.to_thread(resolve_media, request.url, request.mode)
    except Exception as e:
        raise HTTPException(422, public_error(e))


@app.post('/jobs')
def create_job(request: JobRequest):
    cleanup()
    if not request.rights:
        raise HTTPException(400, 'Confirm that you have permission to save this content.')
    if request.mode not in ('video', 'audio', 'playlist') or request.audio not in ('mp3', 'm4a'):
        raise HTTPException(400, 'Unsupported format.')
    if not 1 <= request.quality <= 2160:
        raise HTTPException(400, 'Unsupported quality.')
    if len(set(request.entries)) != len(request.entries):
        raise HTTPException(400, 'Duplicate playlist entries.')
    try:
        normalize_url(request.url, request.mode == 'playlist')
    except ValueError as e:
        raise HTTPException(400, str(e))
    with LOCK:
        if len(JOBS) >= 200 or sum(j['status'] in ('queued', 'downloading', 'processing') for j in JOBS.values()) >= 10:
            raise HTTPException(429, 'The server is busy. Try again in a few minutes.')
        key = secrets.token_hex(16)
        JOBS[key] = {'id': key, 'status': 'queued', 'progress': 0, 'created': time.time(), 'updated': time.time(), 'message': 'Waiting for a processing slot.'}
    POOL.submit(run_job, key, request)
    return {'id': key, 'status': 'queued', 'progress': 0}


@app.get('/jobs/{job_id}')
def read_job(job_id: str):
    cleanup()
    with LOCK:
        j = JOBS.get(job_id)
        if not j:
            raise HTTPException(404, 'This job expired or does not exist.')
        return {k: v for k, v in j.items() if k not in ('file', 'created', 'updated')}


@app.get('/jobs/{job_id}/file')
def read_file(job_id: str):
    cleanup()
    with LOCK:
        j = JOBS.get(job_id)
        if not j or j['status'] != 'ready':
            raise HTTPException(404, 'This file expired or is not ready.')
        path = Path(j['file'])
    if not path.is_file() or ROOT not in path.resolve().parents:
        raise HTTPException(404, 'File unavailable.')
    return FileResponse(path, filename=path.name)


@app.exception_handler(HTTPException)
async def http_error(request, error):
    from fastapi.responses import JSONResponse
    return JSONResponse({'error': error.detail}, status_code=error.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "Invalid request. Check the link, format and selected playlist items."}, status_code=422)
