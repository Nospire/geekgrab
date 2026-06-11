import uuid
import re
import json
import random
import logging
import asyncio
import time
import subprocess
from pathlib import Path
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from services.downloader import download_media, get_platform, is_youtube_music
from database.storage import stats
from services.logger import download_logger
from config import DOWNLOADS_DIR

router = Router()
url_cache = {}

# Рандомные мемные «загрузочные» фразы для первого сообщения-плейсхолдера
STARTING_MESSAGES = [
    "⏳ Поехали...",
    "🚀 Запускаю движки...",
    "🪄 Колдую...",
    "🧙 Шепчу заклинания серверу...",
    "🛠️ Достаю видео из интернета...",
    "🤖 Бип-буп, обрабатываю...",
    "🦾 Напрягаю всю мощь сервера...",
    "🎬 Лезу за твоим видосом...",
    "⚡ Минутку, разгоняюсь...",
    "🐹 Бужу хомячков, крутящих сервер...",
]


def starting_message() -> str:
    return random.choice(STARTING_MESSAGES)

def resolve_user_identity(user: types.User) -> tuple[str, str, str]:
    display_name = user.full_name or user.first_name or "Unknown"
    username = user.username or ""
    stored_name = username or display_name
    handle = f"@{username}" if username else display_name
    return display_name, stored_name, handle

def format_caption(metadata: dict, platform: str, original_url: str = "") -> str:
    """Generate unified caption format for all platforms."""
    uploader = metadata.get('uploader', 'Unknown')
    url = original_url or metadata.get('webpage_url', '')
    
    # Check for verified status in metadata
    is_verified = metadata.get('verified') or metadata.get('creator_is_verified') or metadata.get('uploader_is_verified') or metadata.get('channel_is_verified')
    
    # Strip leading @
    uploader = uploader.lstrip('@')
    # Escape HTML special characters in uploader name
    uploader = uploader.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    if is_verified:
        # Use a combination of a visible emoji and custom tg-emoji if supported
        uploader = f"{uploader} <tg-emoji emoji-id=\"5233582409416448551\">✅</tg-emoji>"

    caption = f"👤 {uploader} | <a href=\"{url}\">Ссылка</a>"
    return caption

async def probe_media_duration_seconds(media_path: Path) -> int:
    def run_probe() -> int:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return 0
            duration_str = result.stdout.strip()
            if not duration_str:
                return 0
            return int(float(duration_str))
        except Exception:
            return 0

    return await asyncio.to_thread(run_probe)


async def probe_media_dimensions(media_path: Path):
    """Probe the real displayed (width, height) of a video via ffprobe.

    yt-dlp/Cobalt metadata can disagree with the actually encoded file, and when
    the width/height we hand Telegram don't match the frame, Telegram squishes
    the player ("схлопывает"). ffprobe reads the true frame size and accounts for
    rotation (portrait phone clips). Returns (w, h) or None on failure.
    """
    def run_probe():
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height:stream_side_data=rotation",
                    "-of", "json",
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout or "{}")
            streams = data.get("streams") or []
            if not streams:
                return None
            st = streams[0]
            w = int(st.get("width") or 0)
            h = int(st.get("height") or 0)
            if w <= 0 or h <= 0:
                return None
            # Account for rotation metadata (rotated portrait videos)
            rotation = 0
            for sd in st.get("side_data_list", []) or []:
                if "rotation" in sd:
                    try:
                        rotation = int(sd.get("rotation") or 0)
                    except (TypeError, ValueError):
                        rotation = 0
            if abs(rotation) % 180 == 90:
                w, h = h, w
            return (w, h)
        except Exception:
            return None

    return await asyncio.to_thread(run_probe)


def _resolve_video_dimensions(probed, metadata: dict):
    """Pick the dimensions to send to Telegram: trust the real file first."""
    if probed:
        return probed
    if metadata.get('width') and metadata.get('height'):
        return int(metadata['width']), int(metadata['height'])
    return None


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! 👋 Кидай ссылку — вытащу видео откуда угодно:\n"
        "• YouTube / YouTube Music 🎵\n"
        "• TikTok\n"
        "• Instagram\n"
        "• Twitter/X\n"
        "• Reddit\n"
        "• Facebook\n"
        "• Vimeo\n"
        "• Twitch\n"
        "• Pinterest\n"
        "• VK / Dailymotion\n"
        "• и ещё 1800+ сайтов!\n\n"
        "🎶 А ещё музыка: Spotify, Apple Music, Tidal, Deezer, Я.Музыка —\n"
        "пришли ссылку на трек, верну MP3 с обложкой и тегами.\n\n"
        "Просто пришли ссылку и наблюдай за магией ✨"
    )

@router.message(lambda m: m.text and not m.text.startswith(('/start', '/panel', '/whitelist', '/unwhitelist', 'add @')))
async def handle_url(message: types.Message):
    # Accept any URL-like string
    url_pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
    
    if not re.search(url_pattern, message.text):
        await message.answer("🤨 Это не ссылка. Кинь нормальную, со словами http и точкой.")
        return
    
    # Validate Pornhub URLs - must have viewkey parameter
    if "pornhub.com" in message.text.lower():
        if "viewkey=" not in message.text:
            await message.answer(
                "❌ Кривая ссылка на Pornhub!\n\n"
                "В ссылке должен быть ID видео (параметр viewkey).\n"
                "Пример: https://www.pornhub.com/view_video.php?viewkey=xxxxx\n\n"
                "Скопируй полную ссылку прямо со страницы видео 😉"
            )
            return

    # Whitelist check
    if stats.whitelisted_users and not stats.is_whitelisted(message.from_user.username):
        await message.answer("⛔ Бот приватный, тебя нет в списке избранных. Постучись к админу 🚪")
        return

    try:
        platform = get_platform(message.text)
        if platform == "unknown":
            await message.answer("🤷 Этот сайт я пока не умею. Попробуй другую ссылку.")
            return

        if platform == "youtube" and not is_youtube_music(message.text):
            request_id = str(uuid.uuid4())[:8]
            url_cache[request_id] = message.text
            
            builder = InlineKeyboardBuilder()
            builder.add(
                InlineKeyboardButton(text="🎵 Аудио (MP3)", callback_data=f"format:audio:{request_id}"),
                InlineKeyboardButton(text="🎥 Видео", callback_data=f"format:video:{request_id}")
            )
            await message.answer("Чё качаем? 👇", reply_markup=builder.as_markup())
            return

        status_message = await message.answer(starting_message())
        
        async def update_status(text: str):
            try:
                await status_message.edit_text(text)
            except Exception:
                pass
        
        is_music = is_youtube_music(message.text) or platform in ("music", "yandexmusic")
        file_path, thumbnail_path, metadata = await download_media(message.text, is_music, progress_callback=update_status)

        # Determine title based on file_path type
        if isinstance(file_path, list):
            title = metadata.get('title', 'TikTok Slideshow')
        else:
            title = file_path.stem

        display_name, stored_name, handle = resolve_user_identity(message.from_user)
        stats.add_active_user(message.from_user.id)
        stats.add_download(
            content_type='Music' if is_music else 'Video',
            user_id=message.from_user.id,
            username=stored_name,
            platform=platform,
            url=message.text,
            title=title
        )

        user_id = message.from_user.id
        download_logger.info(
            f"User: {display_name} ({handle}, ID: {user_id}) | "
            f"Platform: {platform} | "
            f"Type: {'Music' if is_music else 'Video'} | "
            f"URL: {message.text}"
        )

        if isinstance(file_path, list):
            await update_status("📤 Заливаю слайдшоу в Telegram...")
            
            # Separate media types
            image_exts = ['.jpg', '.jpeg', '.png', '.webp']
            video_exts = ['.mp4', '.mov', '.webm', '.mkv']
            audio_exts = ['.mp3', '.m4a', '.wav']

            image_files = [f for f in file_path if f.suffix.lower() in image_exts]
            video_files = [f for f in file_path if f.suffix.lower() in video_exts]
            audio_files = [f for f in file_path if f.suffix.lower() in audio_exts]
            
            # Prepare unified caption
            caption = format_caption(metadata, platform, message.text)

            media_group = []
            ordered_files = sorted(image_files + video_files, key=lambda p: p.name)
            for i, media_path in enumerate(ordered_files):
                caption_text = caption if i == 0 else ""
                parse_mode = 'HTML' if i == 0 else None
                if media_path.suffix.lower() in image_exts:
                    media_item = types.InputMediaPhoto(
                        media=types.FSInputFile(media_path),
                        caption=caption_text,
                        parse_mode=parse_mode
                    )
                else:
                    media_item = types.InputMediaVideo(
                        media=types.FSInputFile(media_path),
                        caption=caption_text,
                        parse_mode=parse_mode,
                        supports_streaming=True
                    )
                media_group.append(media_item)

            # Split into chunks of 10 (Telegram limit)
            if media_group:
                chunk_size = 10
                for i in range(0, len(media_group), chunk_size):
                    chunk = media_group[i:i + chunk_size]
                    await message.answer_media_group(chunk)
            
            # Send audio separately if available
            if audio_files:
                for audio_path in audio_files:
                    try:
                        await message.answer_audio(
                            types.FSInputFile(audio_path)
                        )
                    except Exception as e:
                        logging.error(f"Failed to send audio: {e}")

            # Cleanup
            for photo_path in file_path:
                try:
                    photo_path.unlink()
                except Exception:
                    pass
            await status_message.delete()

        elif file_path.exists():
            await update_status("📤 Заливаю в Telegram...")
            
            # Use unified caption format
            caption = format_caption(metadata, platform, message.text)

            image_exts = ['.jpg', '.jpeg', '.png', '.webp']
            if file_path.suffix.lower() in image_exts:
                await message.answer_photo(
                    types.FSInputFile(file_path),
                    caption=caption,
                    parse_mode='HTML'
                )
                file_path.unlink()
                if thumbnail_path and thumbnail_path.exists():
                    thumbnail_path.unlink()
                await status_message.delete()
                return

            if is_music:
                if thumbnail_path:
                    await message.answer_audio(
                        types.FSInputFile(file_path), 
                        thumbnail=types.FSInputFile(thumbnail_path),
                        duration=int(metadata.get('duration', 0)),
                        caption=caption,
                        parse_mode='HTML'
                    )
                else:
                    await message.answer_audio(
                        types.FSInputFile(file_path),
                        duration=int(metadata.get('duration', 0)),
                        caption=caption,
                        parse_mode='HTML'
                    )
            else:
                duration_value = int(metadata.get('duration', 0))
                if duration_value <= 0:
                    duration_value = await probe_media_duration_seconds(file_path)

                video_kwargs = {
                    'video': types.FSInputFile(file_path),
                    'duration': duration_value,
                    'supports_streaming': True,
                    'caption': caption,
                    'parse_mode': 'HTML'
                }
                
                dims = _resolve_video_dimensions(await probe_media_dimensions(file_path), metadata)
                if dims:
                    video_kwargs['width'], video_kwargs['height'] = dims

                if thumbnail_path:
                   video_kwargs['thumbnail'] = types.FSInputFile(thumbnail_path)

                logging.info(f"Sending video with kwargs: {video_kwargs}")

                # Measure upload time to Telegram
                upload_start = time.time()
                await message.answer_video(**video_kwargs)
                upload_time = time.time() - upload_start
                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                logging.info(f"✅ Video uploaded to Telegram in {upload_time:.1f}s ({file_size_mb:.2f}MB, {file_size_mb/upload_time:.2f}MB/s)")
            
            file_path.unlink()
            if thumbnail_path and thumbnail_path.exists():
                thumbnail_path.unlink()
            await status_message.delete()
        else:
            await status_message.edit_text("😵 Что-то пошло не так при загрузке. Попробуй ещё раз.")
    
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Error: {error_msg}")
        try:
            if "file_path" in locals() and file_path:
                if isinstance(file_path, list):
                    for p in file_path:
                        if p.exists(): p.unlink()
                elif file_path.exists(): file_path.unlink()
            if "thumbnail_path" in locals() and thumbnail_path and thumbnail_path.exists(): thumbnail_path.unlink()
        except Exception:
            pass
        
        # User-friendly error messages
        if "429" in error_msg or "Too Many Requests" in error_msg:
            user_error = "⏳ Reddit сейчас лимитит загрузки (rate-limit) и режет ботов — это не про твою ссылку. Попробуй позже 🙏"
        elif "Unsupported URL" in error_msg:
            user_error = "❌ Эту ссылку я не осилил. Кинь другую 🙏"
        elif "Private video" in error_msg or "Login required" in error_msg:
            user_error = "🔒 Видео приватное или требует логина — мимо."
        elif "Sign in to confirm" in error_msg:
            user_error = "⚠️ YouTube просит куки-авторизацию. Стукни админу бота 🍪"
        else:
            user_error = (
                f"```error\n{error_msg}\n```\n\n"
                f"Если ругается — напиши админу 🛠️"
            )
        
        if 'status_message' in locals():
            await status_message.edit_text(user_error, parse_mode='Markdown' if '```' in user_error else None)
        else:
            await message.answer(user_error, parse_mode='Markdown' if '```' in user_error else None)

@router.callback_query(F.data.startswith("format:"))
async def handle_format_selection(callback: types.CallbackQuery):
    try:
        await callback.answer()
        _, format_type, request_id = callback.data.split(":", 2)
        
        url = url_cache.get(request_id)
        if not url:
            await callback.message.edit_text("⚠️ Запрос протух 🥲 Пришли ссылку заново.")
            return

        if format_type == "video":
            builder = InlineKeyboardBuilder()
            resolutions = [("1080p", 1080), ("720p", 720), ("480p", 480), ("360p", 360)]

            for label, height in resolutions:
                builder.add(InlineKeyboardButton(
                    text=label,
                    callback_data=f"dl_res:{request_id}:{height}"
                ))
            builder.adjust(2)
            
            await callback.message.edit_text("Какое качество? 🎚️", reply_markup=builder.as_markup())
            return

        status_message = await callback.message.edit_text(starting_message())
        
        async def update_status(text: str):
            try:
                await status_message.edit_text(text)
            except Exception:
                pass
        
        try:
            is_music = True
            file_path, thumbnail_path, metadata = await download_media(url, is_music, progress_callback=update_status)

            display_name, stored_name, handle = resolve_user_identity(callback.from_user)
            stats.add_active_user(callback.from_user.id)
            stats.add_download(
                content_type='Music',
                user_id=callback.from_user.id,
                username=stored_name,
                platform='youtube',
                url=url,
                title=file_path.stem
            )

            user_id = callback.from_user.id
            download_logger.info(
                f"User: {display_name} ({handle}, ID: {user_id}) | "
                f"Platform: youtube | "
                f"Type: Audio | "
                f"URL: {url}"
            )

            if file_path.exists():
                await update_status("📤 Заливаю в Telegram...")
                
                caption = format_caption(metadata, 'youtube', url)

                if thumbnail_path:
                    await callback.message.answer_audio(
                        types.FSInputFile(file_path), 
                        thumbnail=types.FSInputFile(thumbnail_path),
                        duration=int(metadata.get('duration', 0)),
                        caption=caption,
                        parse_mode='HTML'
                    )
                else:
                    await callback.message.answer_audio(
                        types.FSInputFile(file_path),
                        duration=int(metadata.get('duration', 0)),
                        caption=caption,
                        parse_mode='HTML'
                    )
                
                file_path.unlink()
                if thumbnail_path and thumbnail_path.exists():
                    thumbnail_path.unlink()
                await status_message.delete()
            else:
                await status_message.edit_text("😵 Что-то пошло не так при загрузке. Попробуй ещё раз.")

        except Exception as e:
            error_msg = str(e)
            logging.error(f"Error: {error_msg}")
            try:
                if "file_path" in locals() and file_path:
                    if isinstance(file_path, list):
                        for p in file_path:
                            if p.exists(): p.unlink()
                    elif file_path.exists(): file_path.unlink()
                if "thumbnail_path" in locals() and thumbnail_path and thumbnail_path.exists(): thumbnail_path.unlink()
            except Exception:
                pass
            
            if "429" in error_msg or "Too Many Requests" in error_msg:
                user_error = "⏳ Reddit сейчас лимитит загрузки (rate-limit) и режет ботов — это не про твою ссылку. Попробуй позже 🙏"
            elif "No working app info" in error_msg or "tiktok:sound" in error_msg:
                user_error = "❌ Ссылки на звук/музыку из TikTok не поддерживаются. Пришли ссылку на само видео 🎥"
            elif "Unsupported URL" in error_msg:
                user_error = "❌ Эта ссылка не поддерживается."
            else:
                user_error = (
                    f"```error\n{error_msg}\n```\n\n"
                    f"Если ругается — напиши админу 🛠️"
                )
            
            await status_message.edit_text(user_error, parse_mode='Markdown' if '```' in user_error else None)

    except Exception as e:
        logging.error(f"Error in callback handling: {str(e)}")
        try:
            await callback.message.answer("❌ Запрос устарел. Попробуй ещё раз 🔁")
        except Exception:
            pass

@router.callback_query(F.data.startswith("dl_res:"))
async def handle_resolution_selection(callback: types.CallbackQuery):
    try:
        await callback.answer()
        _, request_id, height = callback.data.split(":", 2)
        
        url = url_cache.get(request_id)
        if not url:
            await callback.message.edit_text("⚠️ Запрос протух 🥲 Пришли ссылку заново.")
            return

        status_message = await callback.message.edit_text(f"⏳ Качаю видео в {height}p...")
        
        async def update_status(text: str):
            try:
                await status_message.edit_text(text)
            except Exception:
                pass
        
        try:
            file_path, thumbnail_path, metadata = await download_media(
                url,
                is_music=False,
                video_height=int(height),
                progress_callback=update_status
            )

            display_name, stored_name, handle = resolve_user_identity(callback.from_user)
            stats.add_active_user(callback.from_user.id)
            stats.add_download(
                content_type='Video',
                user_id=callback.from_user.id,
                username=stored_name,
                platform='youtube',
                url=url,
                title=file_path.stem
            )

            user_id = callback.from_user.id
            download_logger.info(
                f"User: {display_name} ({handle}, ID: {user_id}) | "
                f"Platform: youtube | "
                f"Type: Video ({height}p) | "
                f"URL: {url}"
            )

            if file_path.exists():
                await update_status("📤 Заливаю в Telegram...")
                
                caption = format_caption(metadata, 'youtube', url)

                duration_value = int(metadata.get('duration', 0))
                if duration_value <= 0:
                    duration_value = await probe_media_duration_seconds(file_path)

                video_kwargs = {
                    'video': types.FSInputFile(file_path),
                    'duration': duration_value,
                    'supports_streaming': True,
                    'caption': caption,
                    'parse_mode': 'HTML'
                }
                
                dims = _resolve_video_dimensions(await probe_media_dimensions(file_path), metadata)
                if dims:
                    video_kwargs['width'], video_kwargs['height'] = dims

                if thumbnail_path:
                   video_kwargs['thumbnail'] = types.FSInputFile(thumbnail_path)

                logging.info(f"Sending video with kwargs: {video_kwargs}")

                # Measure upload time to Telegram
                upload_start = time.time()
                await callback.message.answer_video(**video_kwargs)
                upload_time = time.time() - upload_start
                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                logging.info(f"✅ Video uploaded to Telegram in {upload_time:.1f}s ({file_size_mb:.2f}MB, {file_size_mb/upload_time:.2f}MB/s)")
                
                file_path.unlink()
                if thumbnail_path and thumbnail_path.exists():
                    thumbnail_path.unlink()
                await status_message.delete()
            else:
                await status_message.edit_text("😵 Что-то пошло не так при загрузке. Попробуй ещё раз.")

        except Exception as e:
            error_msg = str(e)
            logging.error(f"Error in video download: {error_msg}")
            
            if "429" in error_msg or "Too Many Requests" in error_msg:
                user_error = "⏳ Reddit сейчас лимитит загрузки (rate-limit) и режет ботов — это не про твою ссылку. Попробуй позже 🙏"
            elif "No working app info" in error_msg or "tiktok:sound" in error_msg:
                user_error = "❌ Ссылки на звук/музыку из TikTok не поддерживаются. Пришли ссылку на само видео 🎥"
            elif "Unsupported URL" in error_msg:
                user_error = "❌ Эта ссылка не поддерживается."
            else:
                user_error = (
                    f"```error\n{error_msg}\n```\n\n"
                    f"Если ругается — напиши админу 🛠️"
                )
            
            await status_message.edit_text(user_error, parse_mode='Markdown' if '```' in user_error else None)

    except Exception as e:
        logging.error(f"Error in resolution callback: {str(e)}")
