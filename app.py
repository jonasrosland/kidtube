import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

CONFIG_PATH = Path(__file__).parent / "config.yaml"

INVIDIOUS_TIMEOUT = 60.0  # Large channels can be slow
_ytdlp_hls_cache: dict[str, tuple[str, float]] = {}


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def live_cams_channel_name(cfg: dict) -> str | None:
    """Display name for the live cams pill, if any cams are configured."""
    if not cfg.get("live_cams"):
        return None
    return cfg.get("live_cams_channel_name", "Live Cams")


def _normalize_video_id(value: str | None) -> str | None:
    """Extract a YouTube video ID from a bare ID or full URL."""
    if not value:
        return None
    value = value.strip()
    if "://" not in value and "/" not in value and "?" not in value:
        return value

    parsed = urlparse(value)
    if parsed.query:
        video_ids = parse_qs(parsed.query).get("v")
        if video_ids:
            return video_ids[0]

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[0] in {"embed", "live", "shorts", "v"} and len(path_parts) > 1:
        return path_parts[1]
    if path_parts and "youtu.be" in parsed.netloc:
        return path_parts[0]

    return value


app = FastAPI()

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def _get_ytdlp_stream_url(video_id: str, *, live: bool = False) -> str | None:
    """Extract best stream URL via yt-dlp (fallback when Invidious unavailable)."""
    fmt = "best" if live else "best[ext=mp4]/best[ext=webm]/best"
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-g",
            "-f", fmt,
            f"https://www.youtube.com/watch?v={video_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        url = stdout.decode().strip()
        return url if url and url.startswith("http") else None
    except Exception:
        return None


async def _get_ytdlp_hls_url(video_id: str, *, force_refresh: bool = False) -> str | None:
    """Get a fresh HLS playlist URL for a live stream, with short-lived cache."""
    now = time.monotonic()
    if not force_refresh and video_id in _ytdlp_hls_cache:
        url, expires = _ytdlp_hls_cache[video_id]
        if expires > now:
            return url
    url = await _get_ytdlp_stream_url(video_id, live=True)
    if url:
        _ytdlp_hls_cache[video_id] = (url, now + 45)
    return url


def _rewrite_hls_playlist(body: str, playlist_url: str, proxy_base: str) -> str:
    """Rewrite HLS playlist URLs to go through our proxy (avoids CORS and broken Invidious proxy)."""
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        absolute = stripped if stripped.startswith("http") else urljoin(playlist_url, stripped)
        lines.append(f"{proxy_base}?url={quote(absolute, safe='')}")
    return "\n".join(lines) + "\n"


def _is_hls_playlist(content_type: str, body: str, url: str) -> bool:
    return (
        "mpegurl" in content_type
        or url.endswith(".m3u8")
        or body.lstrip().startswith("#EXTM3U")
    )


def _is_configured_live_cam(cfg: dict, video_id: str) -> bool:
    for cam in cfg.get("live_cams", []):
        if _normalize_video_id(cam.get("video_id")) == video_id:
            return True
    return False


def _invidious_embeddable(video_info: dict) -> bool:
    """True if Invidious embed player can play this VOD."""
    return bool(video_info.get("formatStreams") or video_info.get("adaptiveFormats"))


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


@app.get("/live-hls/{video_id}")
async def live_hls_proxy(request: Request, video_id: str, url: str | None = None):
    """Proxy live HLS playlists and segments via yt-dlp URLs (Invidious /videoplayback often 403s)."""
    video_id = _normalize_video_id(video_id) or video_id
    proxy_base = f"{str(request.base_url).rstrip('/')}/live-hls/{video_id}"
    fetch_url = url or await _get_ytdlp_hls_url(video_id)
    if not fetch_url:
        return JSONResponse({"error": "Could not extract live stream"}, status_code=502)

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(fetch_url)
        if r.status_code in {403, 410} and not url:
            _ytdlp_hls_cache.pop(video_id, None)
            fetch_url = await _get_ytdlp_hls_url(video_id, force_refresh=True)
            if not fetch_url:
                return JSONResponse({"error": "Live stream expired"}, status_code=502)
            r = await client.get(fetch_url)
        if r.status_code >= 400:
            return JSONResponse({"error": "Upstream stream unavailable"}, status_code=502)

        content_type = r.headers.get("content-type", "")
        data = await r.aread()

    text = data.decode(errors="ignore")
    if _is_hls_playlist(content_type, text, str(r.url)):
        return Response(
            _rewrite_hls_playlist(text, str(r.url), proxy_base),
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    return Response(
        data,
        media_type=content_type or "video/mp2t",
        headers={"Cache-Control": "no-cache"},
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


async def _fetch_video_info(
    client: httpx.AsyncClient,
    base_url: str,
    video_id: str,
) -> dict | None:
    """Fetch video metadata from Invidious."""
    try:
        r = await client.get(f"{base_url.rstrip('/')}/api/v1/videos/{video_id}")
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
        return None


async def _fetch_channel_live_video(
    client: httpx.AsyncClient,
    base_url: str,
    channel_id: str,
) -> dict | None:
    """Return the channel's current live stream, if any."""
    base = base_url.rstrip("/")
    try:
        r = await client.get(
            f"{base}/api/v1/channels/{channel_id}/streams",
            params={"sort_by": "newest"},
        )
        if r.status_code == 200:
            for v in r.json().get("videos", []):
                if v.get("liveNow"):
                    return v
        r = await client.get(f"{base}/api/v1/channels/{channel_id}")
        if r.status_code == 200:
            for v in r.json().get("latestVideos", []):
                if v.get("liveNow"):
                    return v
    except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
        pass
    return None


async def _resolve_live_cam(
    client: httpx.AsyncClient,
    base_url: str,
    cam: dict,
) -> dict:
    """Resolve a live cam config entry to a display card."""
    name = cam.get("name", "Live Cam")
    emoji = cam.get("emoji", "🎥")
    video_id = _normalize_video_id(cam.get("video_id"))
    channel_id = cam.get("channel_id")

    if not video_id and channel_id:
        channel_id = await _resolve_channel_id(client, base_url, channel_id) or channel_id
        live = await _fetch_channel_live_video(client, base_url, channel_id)
        if live:
            video_id = live.get("videoId")
            info = live
        else:
            info = None
    elif video_id:
        info = await _fetch_video_info(client, base_url, video_id)
    else:
        info = None

    thumbnail = cam.get("thumbnail", "")
    title = name
    live_now = False
    if info:
        title = info.get("title", name)
        live_now = bool(info.get("liveNow"))
        if not thumbnail:
            thumbnail = _pick_thumbnail(info.get("videoThumbnails", []), base_url)

    uses_channel = bool(channel_id) and not cam.get("video_id")

    return {
        "id": video_id,
        "name": name,
        "emoji": emoji,
        "title": title,
        "thumbnail": thumbnail,
        "live": live_now if info else bool(video_id and not uses_channel),
        "offline": uses_channel and not video_id,
    }


async def fetch_live_cams(invidious_url: str, cams: list[dict]) -> list[dict]:
    """Resolve all configured live cams via Invidious."""
    if not invidious_url or not cams:
        return []
    async with httpx.AsyncClient(timeout=INVIDIOUS_TIMEOUT) as client:
        return [await _resolve_live_cam(client, invidious_url, cam) for cam in cams]


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
    channels = cfg.get("channels", [])
    kid_name = cfg.get("kid_name", "Aurora")
    cams_channel = live_cams_channel_name(cfg)

    if not invidious_url:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "videos": [],
            "cams": [],
            "channels": channels,
            "live_cams_channel_name": cams_channel,
            "selected_channel": None,
            "selected_playlist": None,
            "selected_playlists": [],
            "is_live_cams": False,
            "kid_name": kid_name,
        })
    count = cfg.get("videos_per_channel", 12)
    min_dur = cfg.get("min_duration_seconds", 60)

    videos: list[dict] = []
    cams: list[dict] = []
    selected_playlists: list[dict] = []
    is_live_cams = bool(cams_channel and channel == cams_channel)

    if is_live_cams:
        cams = await fetch_live_cams(invidious_url, cfg.get("live_cams", []))
    elif channel:
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
        "cams": cams,
        "channels": channels,
        "live_cams_channel_name": cams_channel,
        "selected_channel": channel,
        "selected_playlist": playlist,
        "selected_playlists": selected_playlists,
        "is_live_cams": is_live_cams,
        "kid_name": kid_name,
    })


@app.get("/cams")
async def live_cams_redirect():
    """Legacy URL — live cams live on the home page as a channel pill."""
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote

    cfg = load_config()
    name = live_cams_channel_name(cfg) or "Live Cams"
    return RedirectResponse(url=f"/?channel={quote(name)}", status_code=302)


@app.get("/watch/{video_id}", response_class=HTMLResponse)
async def watch(request: Request, video_id: str, channel: str | None = None):
    video_id = _normalize_video_id(video_id) or video_id
    cfg = load_config()
    invidious_url: str | None = cfg.get("invidious_url", "").rstrip("/") or None
    hls_url: str | None = None
    invidious_embed_url: str | None = None
    stream_url: str | None = None

    video_info: dict | None = None
    if invidious_url:
        async with httpx.AsyncClient(timeout=10) as client:
            video_info = await _fetch_video_info(client, invidious_url, video_id)

    is_live = bool(video_info and video_info.get("liveNow")) or _is_configured_live_cam(cfg, video_id)
    if is_live:
        hls_url = str(request.base_url).rstrip("/") + f"/live-hls/{video_id}"
    elif invidious_url and video_info and _invidious_embeddable(video_info):
        invidious_embed_url = (
            f"{invidious_url}/embed/{video_id}?autoplay=1&related_videos=false&comments=false"
        )
    else:
        url = await _get_ytdlp_stream_url(video_id)
        if url:
            if "m3u8" in url:
                hls_url = url
            else:
                stream_url = str(request.base_url).rstrip("/") + f"/stream-proxy/{video_id}"

    from urllib.parse import quote

    if channel:
        back_href = f"/?channel={quote(channel)}"
        back_label = f"⬅ Back to {channel}"
    else:
        back_href = "/"
        back_label = "⬅ Back to videos"

    return templates.TemplateResponse("watch.html", {
        "request": request,
        "video_id": video_id,
        "hls_url": hls_url,
        "invidious_embed_url": invidious_embed_url,
        "stream_url": stream_url,
        "channels": cfg.get("channels", []),
        "kid_name": cfg.get("kid_name", "Kid"),
        "back_href": back_href,
        "back_label": back_label,
    })
