import asyncio
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from logger import logger
from moviebox.client import MovieBoxClient
from moviebox.models import VideoFileModel
from moviebox.parser import MovieBoxParser
from moviebox.utils import (
    is_deprecation_notice_url,
    resolve_dash_manifest_from_cookie,
)

TITLE_TAG_PATTERN = re.compile(r"\[(.*?)\]|\((.*?)\)")

def extract_dubbings(title: str, corner: str) -> list[str]:
    tags = []
    if corner:
        tags.append(corner.strip())
    matches = TITLE_TAG_PATTERN.findall(title)
    for match in matches:
        for group in match:
            if group:
                tags.append(group.strip())
    seen = set()
    return [x for x in tags if not (x in seen or seen.add(x))]

def clean_title(title: str) -> str:
    cleaned = TITLE_TAG_PATTERN.sub("", title)
    return cleaned.strip()

def extract_match_language_info(item: Any) -> dict[str, Any]:
    return {
        "audio_langs": extract_dubbings(item.title, getattr(item, "corner", ""))
    }

async def find_all_matches(title: str, year: str, is_movie: bool, season: int = 1) -> list[dict[str, Any]]:
    logger.info(f"Searching MovieBox for '{title}' ({year}) - Is Movie: {is_movie}")
    client = MovieBoxClient()
    await client.start()
    
    matches = []
    try:
        parser = MovieBoxParser(client)
        res = await parser.search(title, is_movie, per_page=20)
        logger.info(f"Found {len(res.items)} total search results for '{title}'")
        
        target_title_clean = clean_title(title).lower()
        target_season_title = f"{target_title_clean} s{season}"
        
        for item in res.items:
            item_title_clean = clean_title(item.title).lower()
            logger.info(f"Checking item: {item.title} ({item.year}) vs target {title} ({year})")
            
            if item_title_clean == target_title_clean or item_title_clean == target_season_title:
                if not is_movie or not year or str(item.year) == str(year):
                    logger.info(f"Matched item: {item.title} based on title and year/series rules")
                    base_langs = extract_dubbings(item.title, getattr(item, "corner", ""))
                    matches.append({"item": item, "client": client, "parser": parser, "audio_langs": base_langs})
                    
                    try:
                        detail_res = await client.get(f"/wefeed-mobile-bff/subject-api/get?subjectId={item.subject_id}&update=0&status=0")
                        dubs = detail_res.get("dubs", [])
                        for dub in dubs:
                            dub_id = str(dub.get("subjectId"))
                            dub_name_raw = dub.get("lanName") or dub.get("name")
                            if dub_name_raw:
                                dub_name = dub_name_raw.replace(" dub", "").replace(" Audio", "")
                            else:
                                dub_name = None
                            
                            if dub_id and dub_id != str(item.subject_id):
                                if not any(str(m["item"].subject_id) == dub_id for m in matches):
                                    import copy
                                    mock_item = copy.copy(item)
                                    mock_item.subject_id = int(dub_id) if dub_id.isdigit() else dub_id
                                    matches.append({
                                        "item": mock_item, 
                                        "client": client, 
                                        "parser": parser, 
                                        "audio_langs": [dub_name] if dub_name and dub_name != "Original" else []
                                    })
                                    logger.info(f"Added hidden dubbing match: {dub_name} ({dub_id})")
                    except Exception as e:
                        logger.error(f"Error fetching details for dubbings: {e}")
                    
        logger.info(f"Matched {len(matches)} results based on title and year (including hidden dubs)")
    except Exception as e:
        logger.error(f"Error searching MovieBox: {e}")

    if not matches:
        await client.close()
        
    return matches

async def extract_streams(matches: list[dict[str, Any]], is_movie: bool, season: int = 1, episode: int = 1) -> list[dict[str, Any]]:
    async def fetch_match(match):
        parser = match["parser"]
        item = match["item"]
        all_links = []

        se = 0 if is_movie else season
        ep = 0 if is_movie else episode

        # 1. Primary: Use play-info API (modern MovieBox backend with DASH manifests)
        try:
            info = await parser.get_play_info(str(item.subject_id), season=se, episode=ep)
            streams = info.get("streams", [])
            disp_res = info.get("displayResolutions", "")

            for s in streams:
                raw_url = str(s.get("url", ""))
                cookie = str(s.get("signCookie", ""))
                dash_url = resolve_dash_manifest_from_cookie(cookie)

                if dash_url:
                    play_url = dash_url
                    fmt = "DASH"
                elif not is_deprecation_notice_url(raw_url) and raw_url.startswith("http"):
                    play_url = raw_url
                    fmt = "MP4"
                else:
                    # Deprecated / placeholder notice URL - skip!
                    continue

                # Fetch subtitles for stream if available
                stream_id = str(s.get("id", ""))
                subtitles = []
                try:
                    subtitles = await parser.get_subtitles(str(item.subject_id), stream_id)
                except Exception:
                    pass

                # Parse resolutions list (e.g. "1080,720,480")
                res_str = s.get("resolutions") or disp_res or "1080"
                res_list = []
                for r in str(res_str).split(","):
                    try:
                        res_list.append(int(r.strip()))
                    except ValueError:
                        pass
                if not res_list:
                    res_list = [1080]
                res_list.sort(reverse=True)

                highest_res = res_list[0]
                total_size = int(s.get("size") or 0)
                codec = s.get("codecName") or "hevc"

                for r in res_list:
                    scaled_size = total_size if r == highest_res else int(total_size * ((r / highest_res) ** 1.6))
                    all_links.append(VideoFileModel(
                        url=play_url,
                        resolution=r,
                        size=scaled_size,
                        source_url=raw_url,
                        se=season,
                        ep=episode,
                        format=fmt,
                        codec=codec,
                        sign_cookie=cookie,
                        subtitles=subtitles
                    ))
        except Exception as e:
            logger.error(f"Error fetching play_info for {item.subject_id}: {e}")

        # 2. Fallback: If no play_info streams found, try resource API
        if not all_links:
            try:
                resolutions_to_try = [0, 1080, 720, 480] if is_movie else [1080, 720, 480, 0]
                for res_type in resolutions_to_try:
                    res = await parser.get_download_links(
                        subject_id=str(item.subject_id),
                        resolution=res_type,
                        is_movie=is_movie,
                        season=season,
                        episode=episode
                    )
                    for file_item in res.file_list:
                        if not is_movie:
                            if file_item.se != season or file_item.ep != episode:
                                continue
                        if not is_deprecation_notice_url(str(file_item.url)):
                            all_links.append(file_item)
                    if all_links and is_movie:
                        break
            except Exception as e:
                logger.error(f"Error in fallback resource fetch for {item.subject_id}: {e}")

        return (all_links, match)

    results = await asyncio.gather(*[fetch_match(m) for m in matches])

    if matches:
        try:
            await matches[0]["client"].close()
        except Exception:
            pass

    stream_results = []
    for links, match in results:
        audio_langs = match.get("audio_langs", [])
        logger.info(f"Extracted {len(links)} streams for match: {match['item'].title}")
        for link in links:
            stream_results.append({
                "download": link,
                "audio_langs": audio_langs
            })

    return stream_results

def format_resolution(resolution: int) -> str:
    if resolution >= 2160:
        return "4K"
    elif resolution >= 1080:
        return "1080p"
    elif resolution >= 720:
        return "720p"
    else:
        return f"{resolution}p"

def generate_stream_description(
    size_bytes: int,
    audio_langs: list[str] = None,
    source_url: str = None,
    res_str: str = None,
    format_type: str = "DASH",
    codec: str = "hevc",
) -> str:
    line1_parts = []
    
    if res_str:
        line1_parts.append(f"📺 {res_str}")
        
    if size_bytes:
        size_gb = size_bytes / (1024 ** 3)
        if size_gb >= 1.0:
            line1_parts.append(f"💾 {size_gb:.2f} GB")
        else:
            size_mb = size_bytes / (1024 ** 2)
            line1_parts.append(f"💾 {size_mb:.0f} MB")

    if format_type == "DASH":
        codec_label = codec.upper() if codec else "HEVC"
        line1_parts.append(f"⚡ DASH ({codec_label})")

    lines = []
    if line1_parts:
        lines.append(" | ".join(line1_parts))
        
    if audio_langs:
        LANG_MAP = {
            "ptbr": "🇧🇷 Portuguese (BR)",
            "esla": "🇪🇸 Spanish (LA)",
            "es": "🇪🇸 Spanish",
            "en": "🇺🇸 English",
            "hindi": "🇮🇳 Hindi",
            "telugu": "🇮🇳 Telugu",
            "tamil": "🇮🇳 Tamil",
            "kannada": "🇮🇳 Kannada",
            "malayalam": "🇮🇳 Malayalam",
            "ko": "🇰🇷 Korean",
            "ja": "🇯🇵 Japanese",
            "fr": "🇫🇷 French",
            "de": "🇩🇪 German",
            "it": "🇮🇹 Italian",
            "ru": "🇷🇺 Russian",
            "tr": "🇹🇷 Turkish",
            "ar": "🇸🇦 Arabic",
            "th": "🇹🇭 Thai",
            "id": "🇮🇩 Indonesian",
            "original": "🇺🇸 English (Original)",
            "english": "🇺🇸 English"
        }
        
        nice_langs = []
        for lang in audio_langs:
            clean_lang = lang.lower().strip()
            if clean_lang in LANG_MAP:
                nice_langs.append(LANG_MAP[clean_lang])
            else:
                nice_langs.append(lang.capitalize())
                
        langs_str = ", ".join(nice_langs)
        lines.append(f"🔊 {langs_str}")

    return "\n".join(lines)

def get_stream_filename(url: str) -> str:
    url_str = str(url).lower()
    for ext in ("mpd", "mp4", "mkv", "avi", "webm", "m4v", "mov", "ts"):
        if f".{ext}" in url_str:
            return f"stream.{ext}"
    return "stream.mpd" if "dash" in url_str else "stream.mp4"

