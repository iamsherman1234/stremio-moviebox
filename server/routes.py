import base64
import json
import re
from typing import Annotated
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import StreamingResponse

from logger import logger
from server.manifest import Manifest, get_manifest
from streaming.metadata import resolve_imdb_id
from streaming.provider import (
    extract_streams,
    find_all_matches,
    format_resolution,
    generate_stream_description,
    get_stream_filename,
)

router = APIRouter()

def parse_config(config_str: str) -> dict:
    try:
        padding = 4 - (len(config_str) % 4)
        if padding != 4:
            config_str += "=" * padding
        decoded = base64.urlsafe_b64decode(config_str).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return {}

@router.get("/{config}/manifest.json")
async def manifest_endpoint(request: Request, config: str) -> Manifest:
    manifest = get_manifest()
    manifest.logo = str(request.base_url) + "logo.png"
    return manifest

@router.get("/manifest.json")
async def manifest_endpoint_no_config(request: Request) -> Manifest:
    manifest = get_manifest()
    manifest.logo = str(request.base_url) + "logo.png"
    return manifest

@router.get("/proxy/stream")
async def proxy_stream(request: Request, d: str):
    headers = {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "*/*",
    }
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
    try:
        req = client.build_request("GET", d, headers=headers)
        resp = await client.send(req, stream=True)

        response_headers = {
            "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }
        if "Content-Length" in resp.headers:
            response_headers["Content-Length"] = resp.headers["Content-Length"]
        if "Content-Range" in resp.headers:
            response_headers["Content-Range"] = resp.headers["Content-Range"]

        async def stream_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=resp.status_code,
            headers=response_headers
        )
    except Exception as e:
        await client.aclose()
        logger.error(f"Proxy stream error for {d}: {e}")
        raise HTTPException(status_code=502, detail="Stream proxy error")

@router.get("/{config}/stream/{type}/{id}.json")
async def stream_endpoint_with_config(
    request: Request,
    config: str,
    type: Annotated[str, Path(...)],
    id: Annotated[str, Path(...)],
):
    return await handle_stream(request, type, id, config)

@router.get("/stream/{type}/{id}.json")
async def stream_endpoint(
    request: Request,
    type: Annotated[str, Path(...)],
    id: Annotated[str, Path(...)],
):
    return await handle_stream(request, type, id, "")

async def handle_stream(request: Request, type: str, id: str, config_str: str):
    logger.info(f"Received stream request: type={type}, id={id}")
    if type not in ["movie", "series"]:
        raise HTTPException(status_code=404, detail="Unsupported type")

    config = parse_config(config_str)
    min_res = config.get("resolution", "all")
    layout = config.get("layout", "cinematic")

    parts = id.split(":")
    imdb_id = parts[0]
    season = 1
    episode = 1

    if type == "series" and len(parts) >= 3:
        season = int(parts[1])
        episode = int(parts[2])

    meta = await resolve_imdb_id(type, imdb_id)
    title = meta.get("name")
    
    logger.info(f"Cinemeta resolved ID {imdb_id} to title: '{title}'")

    if not title:
        logger.warning(f"Failed to resolve title for {imdb_id}")
        return {"streams": []}

    year_match = re.search(r"\d{4}", str(meta.get("releaseInfo", ""))) or re.search(r"\d{4}", str(meta.get("year", "")))
    year = year_match.group(0) if year_match else ""

    matches = await find_all_matches(title, year, is_movie=(type == "movie"), season=season)
    if not matches:
        return {"streams": []}

    stream_results = await extract_streams(matches, type == "movie", season, episode)
    
    logger.info(f"Total streams extracted before filtering: {len(stream_results)}")

    def sort_key(x):
        return (x["download"].resolution, len(x.get("audio_langs", [])))

    stream_results.sort(key=sort_key, reverse=True)

    merged_streams = {}

    for stream_data in stream_results:
        dl = stream_data["download"]
        audio_langs = stream_data.get("audio_langs", [])

        url_str = str(dl.url)
        base_dl_url = url_str.split("?")[0] if "?" in url_str else url_str
        
        if base_dl_url not in merged_streams:
            merged_streams[base_dl_url] = {
                "dl": dl,
                "url_str": url_str,
                "audio_langs": set(audio_langs)
            }
        else:
            merged_streams[base_dl_url]["audio_langs"].update(audio_langs)

    base_url = str(request.base_url)

    streams = []
    for base_dl_url, data in merged_streams.items():
        dl = data["dl"]
        url_str = data["url_str"]
        audio_langs = list(data["audio_langs"])

        resolution = dl.resolution
        size = dl.size

        if min_res == "4k" and resolution < 2160:
            continue
        elif min_res == "1080p" and resolution < 1080:
            continue
        elif min_res == "720p" and resolution < 720:
            continue

        filename = get_stream_filename(url_str)
        res_str = format_resolution(resolution)

        desc = generate_stream_description(
            size,
            audio_langs=audio_langs,
            source_url=dl.source_url,
            res_str=res_str
        )

        domain = ""
        try:
            netloc = urlparse(str(dl.source_url)).netloc
            if netloc:
                domain = netloc.replace("www.", "").split(".")[0].capitalize()
        except Exception:
            pass
            
        name_str = "MovieBox"
        if domain:
            name_str += f"\n🌐 {domain}"

        if layout == "torrentio":
            desc = desc.replace(" | ", "\n")

        # 1. Direct Stream with okhttp User-Agent proxyHeaders (for Stremio apps)
        streams.append({
            "name": name_str,
            "title": desc,
            "url": url_str,
            "behaviorHints": {
                "notWebReady": False,
                "filename": filename,
                "proxyHeaders": {
                    "request": {
                        "User-Agent": "okhttp/4.12.0",
                        "Accept": "*/*"
                    }
                }
            },
        })

        # 2. Proxied Stream option via Addon Stream Proxy (for Web / CORS / clients blocking custom headers)
        proxy_stream_url = f"{base_url}proxy/stream?d={quote(url_str)}"
        streams.append({
            "name": f"{name_str} [Proxy]",
            "title": desc,
            "url": proxy_stream_url,
            "behaviorHints": {
                "notWebReady": False,
                "filename": filename,
            },
        })

    return {"streams": streams}
