<div align="center">
  <img src="assets/logo-bg.svg" alt="GeekGrab" width="160" height="160">

  <h1>GeekGrab</h1>

  <p><b>Телеграм-бот, который вытаскивает видео и музыку откуда угодно.</b><br>
  Кидаешь ссылку — получаешь файл. 1800+ сайтов, движок yt-dlp с фолбэком на Cobalt.</p>
</div>

---

## Что умеет

- 🎥 **Видео и 🎵 аудио (MP3)** — на выбор для YouTube.
- 🎶 **Музыка из Spotify / Apple Music / Tidal / Deezer / Я.Музыки** — присылаешь ссылку на трек, получаешь MP3 с обложкой и тегами. Spotify качается напрямую в **320 kbps** через zotify (нужен `credentials.json` + Premium), Apple/Tidal/Deezer — через odesli→YouTube (spotdl), Я.Музыка — нативно через yt-dlp.
- 🎚️ **Выбор качества** — 1080p / 720p / 480p / 360p.
- 🧩 **Два движка с фолбэком** — сначала [yt-dlp](https://github.com/yt-dlp/yt-dlp), если не вышло — [Cobalt](https://github.com/imputnet/cobalt). Что-нибудь да вытащит.
- 📦 **Большие файлы** — свой локальный Telegram Bot API сервер, поэтому отправка не упирается в лимит 50 МБ.
- 🛡️ **Антибот-обход** — Chrome-импersonation и TLS-fingerprint (`curl-cffi`) для капризных сайтов.
- 🔒 **Приватный режим** — whitelist по username.
- 😎 **Мемные статусы** — пока качает, бот развлекает («Отбиваюсь от ФБР…», «Бужу хомячков в колесе сервера…»).
- 🖼️ **Правильные пропорции** — реальные размеры видео берутся из файла через ffprobe, Telegram больше не «схлопывает» ролики.

## Поддерживаемые платформы

YouTube / YouTube Music · TikTok · Instagram · Twitter/X · Reddit · Facebook · Vimeo · Twitch · Pinterest · VK · Dailymotion · Pornhub · RedGIFs · Pikabu · Rule34Video — **и ещё 1800+ сайтов**.

## Стек

| Слой | Технология |
|---|---|
| Бот | Python 3.13, [aiogram 3](https://github.com/aiogram/aiogram) |
| Загрузка | yt-dlp + Cobalt API |
| Telegram API | локальный `aiogram/telegram-bot-api` (файлы >50 МБ) |
| База | PostgreSQL 16 |
| Инфра | Docker Compose, Watchtower (авто-апдейт), ffmpeg |

## Быстрый старт

```bash
git clone https://github.com/Nospire/geekgrab.git
cd geekgrab

# 1. Заполни переменные окружения
cp .env.example .env
nano .env            # токен бота, API ID/HASH, пароль БД, ключ Cobalt

# 2. Подними весь стек
docker compose up -d --build

# 3. Логи
docker compose logs -f bot
```

Бот, Cobalt API, локальный Telegram Bot API и Postgres поднимутся одной командой.

## Конфигурация (`.env`)

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен бота от [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | с [my.telegram.org](https://my.telegram.org) — для локального Bot API |
| `ADMIN_USERNAME` | username админа (без `@`) |
| `WHITELISTED` | список разрешённых username через запятую (пусто = бот открыт всем) |
| `COBALT_API_KEY` | ключ доступа к Cobalt API |
| `COOKIES_CONTENT` | куки в формате Netscape для приватного/возрастного контента |
| `SOCKS_PROXY` / `HTTP_PROXY` | прокси для обхода блокировок (опционально) |

Полный список — в [`.env.example`](.env.example).

## Лицензия

[MIT](LICENSE). Форк на основе открытого проекта, поддерживается [Nospire](https://github.com/Nospire).
