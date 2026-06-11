"""GeekGrab — музыкальный модуль (Путь A: метаданные → YouTube → теги).

Стримы Spotify/Apple/Tidal/Deezer защищены DRM и напрямую не качаются. Здесь мы:
  • Spotify           → spotdl напрямую (URL → метаданные → матч на YouTube Music)
  • Apple/Tidal/Deezer → odesli (song.link) даёт «артист — трек» → spotdl по строке
  • Я.Музыка          → обрабатывается нативным экстрактором yt-dlp (не здесь)

Возвращаем тот же кортеж, что и download_media: (mp3_path, cover_path|None, metadata),
чтобы существующий путь отправки answer_audio подхватил файл без изменений.
"""

import json
import uuid
import asyncio
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple, Dict

# Хосты музыкальных сервисов → внутреннее имя
MUSIC_HOSTS = {
    "open.spotify.com": "spotify",
    "spotify.link": "spotify",
    "music.apple.com": "apple",
    "tidal.com": "tidal",
    "listen.tidal.com": "tidal",
    "deezer.com": "deezer",
    "deezer.page.link": "deezer",
    "music.yandex.ru": "yandex",
    "music.yandex.com": "yandex",
    "music.yandex.kz": "yandex",
}

# Сервисы, которые тянем через spotdl (Я.Музыка идёт по ветке yt-dlp отдельно)
SPOTDL_SERVICES = {"spotify", "apple", "tidal", "deezer"}


def detect_music_service(url: str) -> Optional[str]:
    """Вернёт имя музыкального сервиса по URL или None."""
    u = (url or "").lower()
    for host, svc in MUSIC_HOSTS.items():
        if host in u:
            return svc
    return None


def _odesli_metadata(url: str) -> Optional[str]:
    """Резолвит любую музыкальную ссылку в строку 'Артист - Трек' через song.link."""
    api = "https://api.song.link/v1-alpha.1/links?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0 (GeekGrab)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    entities = data.get("entitiesByUniqueId", {}) or {}
    song = None
    for v in entities.values():
        if v.get("type") == "song":
            song = v
            break
    if song is None and entities:
        song = next(iter(entities.values()))
    if not song:
        return None
    title = song.get("title")
    artist = song.get("artistName")
    if title and artist:
        return f"{artist} - {title}"
    return title or None


def _build_result(mp3: Path) -> Tuple[Path, Optional[Path], Dict]:
    """Достаёт теги/обложку/длительность из готового mp3 для отправки в Telegram."""
    meta: Dict = {"title": mp3.stem, "duration": 0, "webpage_url": ""}
    cover_path: Optional[Path] = None
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3
        audio = MP3(str(mp3), ID3=ID3)
        tags = audio.tags
        if tags is not None:
            if tags.get("TIT2"):
                meta["title"] = str(tags.get("TIT2"))
            if tags.get("TPE1"):
                meta["uploader"] = str(tags.get("TPE1"))
            if tags.get("TALB"):
                meta["album"] = str(tags.get("TALB"))
            apics = tags.getall("APIC")
            if apics:
                cover_path = _save_cover(apics[0].data, mp3)
        meta["duration"] = int(audio.info.length)
    except Exception as e:
        logging.warning(f"[MUSIC] не смог прочитать теги {mp3.name}: {e}")
    return mp3, cover_path, meta


def _save_cover(data: bytes, mp3: Path) -> Optional[Path]:
    """Сохраняет обложку как jpg ≤320px (требование Telegram к превью аудио)."""
    cover = mp3.with_suffix(".cover.jpg")
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((320, 320))
        img.save(cover, "JPEG", quality=85)
        return cover
    except Exception as e:
        logging.warning(f"[MUSIC] обложка не обработалась: {e}")
        try:
            cover.write_bytes(data)
            return cover
        except Exception:
            return None


async def _spotdl_download(query: str, work_dir: Path, progress_callback=None) -> Tuple[Path, Optional[Path], Dict]:
    if progress_callback:
        await progress_callback("⬇️ Качаю трек...")
    template = str(work_dir / "{artists} - {title}.{output-ext}")
    proc = await asyncio.create_subprocess_exec(
        "spotdl", "download", query,
        "--output", template,
        "--format", "mp3",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    log = (out or b"").decode("utf-8", "ignore")
    logging.info(f"[MUSIC] spotdl finished rc={proc.returncode}: {log[-400:]}")
    mp3s = sorted(work_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mp3s:
        raise RuntimeError("Трек не найден или не скачался. Попробуй другую ссылку.")
    return _build_result(mp3s[0])


# Путь B для Spotify: настоящее качество (до 320k) через zotify + credentials.json.
ZOTIFY_CREDS = "/app/data/spotify_credentials.json"


async def _zotify_download(url: str, work_dir: Path, progress_callback=None) -> Tuple[Path, Optional[Path], Dict]:
    """Качает трек напрямую со Spotify в mp3 320k (нужен credentials.json + Premium)."""
    if progress_callback:
        await progress_callback("⬇️ Качаю со Spotify в максимальном качестве...")
    cmd = [
        "zotify",
        "--creds", ZOTIFY_CREDS,
        "--root-path", str(work_dir),
        "--codec", "mp3", "-q", "very_high",
        "-ie", "false", "-ip", "false",
        "--disable-song-archive", "true",
        "--disable-directory-archives", "true",
        "--download-lyrics", "false",
        "--download-real-time", "false",
        "--print-splash", "false",
        "--print-progress-info", "false",
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=150)
    log = (out or b"").decode("utf-8", "ignore")
    logging.info(f"[MUSIC] zotify rc={proc.returncode}: {log[-300:]}")
    mp3s = sorted(work_dir.rglob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mp3s:
        raise RuntimeError("zotify не отдал mp3")
    return _build_result(mp3s[0])


async def download_music(url: str, downloads_dir, progress_callback=None) -> Tuple[Path, Optional[Path], Dict]:
    """Главная точка входа: качает трек по музыкальной ссылке, возвращает (mp3, cover, meta)."""
    svc = detect_music_service(url)
    if svc not in SPOTDL_SERVICES:
        raise RuntimeError(f"Сервис не поддерживается музыкальным модулем: {svc}")

    work_dir = Path(downloads_dir) / f"music_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Spotify: сначала пробуем настоящее качество через zotify, при сбое — spotdl/YouTube.
    if svc == "spotify" and Path(ZOTIFY_CREDS).exists():
        try:
            mp3, cover, metadata = await _zotify_download(url, work_dir, progress_callback)
            metadata.setdefault("webpage_url", url)
            return mp3, cover, metadata
        except Exception as e:
            logging.warning(f"[MUSIC] zotify не смог ({e}); фолбэк на spotdl")
            if progress_callback:
                await progress_callback("↩️ Беру с YouTube (фолбэк)...")

    if svc == "spotify":
        query = url
    else:
        if progress_callback:
            await progress_callback("🔎 Определяю трек по ссылке...")
        meta = await asyncio.to_thread(_odesli_metadata, url)
        if not meta:
            raise RuntimeError("Не удалось определить трек по этой ссылке.")
        query = meta

    mp3, cover, metadata = await _spotdl_download(query, work_dir, progress_callback)
    metadata.setdefault("webpage_url", url)
    return mp3, cover, metadata
