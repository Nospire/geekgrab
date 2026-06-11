"""GeekGrab — скрейпер Pikabu (pikabu.ru).

Pikabu нет в yt-dlp. Видео в посте размечено атрибутами data-webm / data-source
на CDN cs*.pikabu.ru; mp4 = база data-source + ".mp4". CDN иногда тупит на первом
коннекте, поэтому фетч и скачивание идут с ретраями. Пост может содержать несколько
видео — тогда возвращаем список путей (существующая отправка шлёт их группой).

Возвращает (file_or_list, thumbnail|None, metadata) — как download_media.
"""

import re
import uuid
import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Union

import requests

PIKABU_HOSTS = ("pikabu.ru", "pikabu.monster")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def is_pikabu(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in PIKABU_HOSTS)


def _fetch_text(url: str, timeout: int = 30, retries: int = 3) -> str:
    """GET страницы Pikabu (cp1251) с ретраями — CDN/сайт бывают флапают."""
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
            r.raise_for_status()
            # Pikabu отдаёт windows-1251
            return r.content.decode("cp1251", "ignore")
        except Exception as e:
            last_err = e
            logging.warning(f"[PIKABU] fetch попытка {attempt + 1}/{retries} не удалась: {e}")
    raise RuntimeError(f"Не удалось загрузить страницу Pikabu: {last_err}")


def _download_file(url: str, dest: Path, timeout: int = 60, retries: int = 3) -> Path:
    last_err = None
    for attempt in range(retries):
        try:
            with requests.get(url, headers={"User-Agent": _UA}, timeout=timeout, stream=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
            if dest.stat().st_size > 0:
                return dest
            raise RuntimeError("пустой файл")
        except Exception as e:
            last_err = e
            logging.warning(f"[PIKABU] download попытка {attempt + 1}/{retries} ({url[:70]}): {e}")
    raise RuntimeError(f"Не удалось скачать видео с Pikabu: {last_err}")


def _extract(pattern: str, html: str) -> Optional[str]:
    m = re.search(pattern, html)
    return m.group(1) if m else None


def _parse_video_urls(html: str) -> List[str]:
    """Находит mp4-ссылки видео в посте. data-webm однозначно метит видео."""
    urls: List[str] = []
    # 1) основной сигнал — data-webm: база.webm → база.mp4
    for webm in re.findall(r'data-webm="(https://cs\d+\.pikabu\.ru/[^"]+)\.webm"', html):
        urls.append(webm + ".mp4")
    # 2) фолбэк — data-source у блоков video-file (на случай отсутствия webm)
    if not urls:
        for src in re.findall(r'data-type="video-file"[^>]*?data-source="(https://cs\d+\.pikabu\.ru/[^"]+)"', html):
            urls.append(src + ".mp4")
    # дедуп с сохранением порядка
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _parse_metadata(html: str, url: str) -> Dict:
    title = _extract(r'<meta property="og:title" content="([^"]*)"', html) or "Pikabu"
    desc = _extract(r'<meta property="og:description" content="([^"]*)"', html) or ""
    author = _extract(r'пикабушника\s+([^\s]+?)\s+в сообществе', desc) \
        or _extract(r'/@([A-Za-z0-9._-]+)', html) \
        or "Pikabu"
    return {"title": title, "uploader": author, "webpage_url": url}


async def download_pikabu(url: str, downloads_dir, progress_callback=None) -> Tuple[Union[Path, List[Path]], Optional[Path], Dict]:
    """Качает видео(а) из поста Pikabu."""
    if progress_callback:
        await progress_callback("🔎 Разбираю пост Pikabu...")

    html = await asyncio.to_thread(_fetch_text, url)
    video_urls = _parse_video_urls(html)
    metadata = _parse_metadata(html, url)

    if not video_urls:
        raise RuntimeError("В этом посте Pikabu не нашлось видео.")

    work_dir = Path(downloads_dir) / f"pikabu_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    if progress_callback:
        await progress_callback("⬇️ Качаю видео с Pikabu...")

    files: List[Path] = []
    for i, vurl in enumerate(video_urls):
        dest = work_dir / f"pikabu_{i + 1}.mp4"
        files.append(await asyncio.to_thread(_download_file, vurl, dest))

    result: Union[Path, List[Path]] = files if len(files) > 1 else files[0]
    return result, None, metadata
