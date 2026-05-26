import asyncio
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

CONFIG_PATH = Path(__file__).parent / "config.yaml"

INVIDIOUS_TIMEOUT = 60.0  # Large channels can be slow


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


app = FastAPI()

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def _get_ytdlp_stream_url(video_id: str) -> str | None:
    """Extract best stream URL via yt-dlp (fallback when Invidious unavailable)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-g",
            "-f", "best[ext=mp4]/best[ext=webm]/best",
            f"https://www.youtube.com/watch?v={video_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        url = stdout.decode().strip()
        return url if url and url.startswith("http") else None
    except Exception:
        return None


@app.get("/stream-proxy/{video_id}")
async def stream_proxy(video_id: str, request: Request):
    """Proxy video stream via yt-dlp (fallback when Invidious unavailable)."""
    url = await _get_ytdlp_stream_url(video_id)
    if not url:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Could not extract stream"}, status_code=502)
    range_header = request.headers.get("range")
    req_headers = {"Range": range_header} if range_header else {}

    async def stream_gen():
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=req_headers) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        stream_gen(),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


def _format_duration(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _pick_thumbnail(thumbnails: list[dict], base_url: str) -> str:
    """Pick best thumbnail and make absolute URL."""
    for q in ("high", "medium", "sddefault", "maxresdefault", "default"):
        for t in thumbnails:
            if t.get("quality") == q:
                url = t.get("url", "")
                if url.startswith("/"):
                    return base_url.rstrip("/") + url
                return url
    return thumbnails[0]["url"] if thumbnails else ""


def _video_to_card(v: dict, channel_name: str, base_url: str) -> dict:
    """Convert Invidious video object to our card format."""
    pub = v.get("published")
    published_display = v.get("publishedText", "") or (str(pub)[:10] if pub else "")
    video_id = v.get("videoId") or (
        v.get("playlistId") if v.get("type") == "playlist" else ""
    )
    return {
        "id": video_id,
        "title": v.get("title", ""),
        "thumbnail": _pick_thumbnail(v.get("videoThumbnails", []), base_url),
        "channel": channel_name,
        "published": published_display,
        "published_ts": pub or 0,
        "duration": _format_duration(v.get("lengthSeconds", 0)),
    }


def _is_channel_id(identifier: str) -> bool:
    """True if this looks like a YouTube channel ID (UC...)."""
    return identifier.startswith("UC") and len(identifier) == 24


def _uploads_playlist_id(channel_id: str) -> str:
    """Channel uploads playlist ID (UU + same suffix as UC channel id)."""
    if _is_channel_id(channel_id):
        return "UU" + channel_id[2:]
    return channel_id


def _channel_videos_are_mislabeled(items: list[dict]) -> bool:
    """Invidious sometimes returns uploads as type playlist with video IDs in playlistId."""
    if not items:
        return False
    sample = items[0]
    return (
        sample.get("type") == "playlist"
        and not sample.get("videoId")
        and bool(sample.get("playlistId"))
    )


async def _resolve_channel_id(client: httpx.AsyncClient, base_url: str, identifier: str) -> str | None:
    """Resolve handle (@peppapigtales) or channel ID via Invidious. Returns ucid or None."""
    identifier = identifier.strip()
    if identifier.startswith("@"):
        identifier = identifier[1:]
    if _is_channel_id(identifier):
        return identifier
    try:
        url = f"{base_url.rstrip('/')}/api/v1/resolveurl"
        r = await client.get(url, params={"url": f"https://www.youtube.com/@{identifier}"})
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("ucid")
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
        return None


async def _fetch_playlist_invidious(
    client: httpx.AsyncClient,
    base_url: str,
    channel_name: str,
    playlist_id: str,
    count: int,
    min_duration: int,
    max_duration: int | None = None,
) -> list[dict]:
    """Fetch playlist videos from Invidious, sorted by index (episode order)."""
    videos: list[dict] = []
    page = 1
    try:
        while len(videos) < count:
            r = await client.get(
                f"{base_url.rstrip('/')}/api/v1/playlists/{playlist_id}",
                params={"page": page},
            )
            if r.status_code != 200:
                break
            data = r.json()
            pl_videos = data.get("videos", [])
            if not pl_videos:
                break
            for v in pl_videos:
                if len(videos) >= count:
                    break
                length = v.get("lengthSeconds", 0)
                if length < min_duration:
                    continue
                if max_duration is not None and length > max_duration:
                    continue
                videos.append(_video_to_card(v, channel_name, base_url))
            if len(pl_videos) < 100:  # Invidious typically returns up to 100 per page
                break
            page += 1
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
        pass
    return videos


async def _fetch_channel_videos_invidious(
    client: httpx.AsyncClient,
    base_url: str,
    channel_name: str,
    channel_id: str,
    count: int,
    min_duration: int,
) -> list[dict]:
    """Fetch channel uploads from Invidious, filter by duration (excludes Shorts)."""
    videos: list[dict] = []
    continuation = None
    try:
        while len(videos) < count:
            url = f"{base_url.rstrip('/')}/api/v1/channels/{channel_id}/videos"
            params = {"sort_by": "newest"}
            if continuation:
                params["continuation"] = continuation
            r = await client.get(url, params=params)
            if r.status_code != 200:
                break
            data = r.json()
            ch_videos = data.get("videos", [])
            if not ch_videos:
                break
            if _channel_videos_are_mislabeled(ch_videos):
                return await _fetch_playlist_invidious(
                    client,
                    base_url,
                    channel_name,
                    _uploads_playlist_id(channel_id),
                    count,
                    min_duration,
                )
            for v in ch_videos:
                if len(videos) >= count:
                    break
                length = v.get("lengthSeconds", 0)
                if length < min_duration:
                    continue
                videos.append(_video_to_card(v, channel_name, base_url))
            continuation = data.get("continuation")
            if not continuation:
                break
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
        pass
    return videos


async def fetch_channel_videos(
    channel: dict,
    invidious_url: str,
    count: int,
    min_duration: int,
    playlist_id: str | None = None,
) -> list[dict]:
    """Fetch videos via Invidious. Uses playlist(s) if specified, else channel uploads."""
    if "playlists" in channel and playlist_id:
        pl = next((p for p in channel["playlists"] if p["id"] == playlist_id), None)
        if pl:
            async with httpx.AsyncClient(timeout=INVIDIOUS_TIMEOUT) as client:
                return await _fetch_playlist_invidious(
                    client,
                    invidious_url,
                    channel["name"],
                    pl["id"],
                    count,
                    min_duration,
                    max_duration=pl.get("max_duration_seconds"),
                )
    if "playlist" in channel:
        async with httpx.AsyncClient(timeout=INVIDIOUS_TIMEOUT) as client:
            return await _fetch_playlist_invidious(
                client,
                invidious_url,
                channel["name"],
                channel["playlist"],
                count,
                min_duration,
            )

    async with httpx.AsyncClient(timeout=INVIDIOUS_TIMEOUT) as client:
        channel_id = await _resolve_channel_id(client, invidious_url, channel["id"])
        if not channel_id:
            return []
        return await _fetch_channel_videos_invidious(
            client,
            invidious_url,
            channel["name"],
            channel_id,
            count,
            min_duration,
        )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, channel: str | None = None, playlist: str | None = None):
    cfg = load_config()
    invidious_url = cfg.get("invidious_url", "").rstrip("/")
    if not invidious_url:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "videos": [],
            "channels": cfg.get("channels", []),
            "selected_channel": None,
            "selected_playlist": None,
            "selected_playlists": [],
            "kid_name": cfg.get("kid_name", "Aurora"),
        })
    count = cfg.get("videos_per_channel", 12)
    min_dur = cfg.get("min_duration_seconds", 60)
    channels = cfg.get("channels", [])
    kid_name = cfg.get("kid_name", "Aurora")

    videos: list[dict] = []
    selected_playlists: list[dict] = []
    if channel:
        ch = next((c for c in channels if c["name"] == channel), None)
        if ch:
            if "playlists" in ch:
                selected_playlists = ch["playlists"]
                if playlist:
                    videos = await fetch_channel_videos(ch, invidious_url, count, min_dur, playlist_id=playlist)
            elif "playlist" in ch:
                videos = await fetch_channel_videos(ch, invidious_url, count, min_dur)
            else:
                videos = await fetch_channel_videos(ch, invidious_url, count, min_dur)
                videos = sorted(videos, key=lambda v: v.get("published_ts", 0), reverse=True)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "videos": videos,
        "channels": channels,
        "selected_channel": channel,
        "selected_playlist": playlist,
        "selected_playlists": selected_playlists,
        "kid_name": kid_name,
    })


@app.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch(request: Request, video_id: str):
    cfg = load_config()
    invidious_url: str | None = cfg.get("invidious_url", "").rstrip("/") or None
    stream_url: str | None = None
    if not invidious_url or not await _invidious_available(invidious_url, video_id):
        url = await _get_ytdlp_stream_url(video_id)
        if url:
            stream_url = str(request.base_url).rstrip("/") + f"/stream-proxy/{video_id}"

    return templates.TemplateResponse("watch.html", {
        "request": request,
        "video_id": video_id,
        "invidious_embed_url": f"{invidious_url}/embed/{video_id}?autoplay=1&related_videos=false&comments=false" if invidious_url else None,
        "stream_url": stream_url,
        "channels": cfg.get("channels", []),
        "kid_name": cfg.get("kid_name", "Kid"),
    })


async def _invidious_available(base_url: str, video_id: str) -> bool:
    """Check if Invidious can serve this video."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/v1/videos/{video_id}")
            if r.status_code != 200:
                return False
            data = r.json()
            return "formatStreams" in data or "adaptiveFormats" in data
    except Exception:
        return False
