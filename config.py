# ============================================================================
# ===================  НАСТРОЙКИ — ВПИШИ СВОИ ЗНАЧЕНИЯ СЮДА  ===============
# ============================================================================

TELEGRAM_TOKEN = "8952832927:AAGvm94eVz1bzT0pzYctUvQ_dEJzZKaajy4"

# Если оставить пустой строкой "" — бот будет отвечать в любом чате.
# После первого /start бот пришлёт твой chat_id, впиши его сюда и задеплой заново.
ALLOWED_CHAT_ID = "6716942872"

BYBIT_API_KEY = "HoWywKrAcWZGHpnxXY"
BYBIT_API_SECRET = "Jy5QRuFnhANj2TX1HMnqRY3m0JWclidsNLfY"

BYBIT_TESTNET = False            # True — тестовая сеть Bybit, False — реальная
BYBIT_CATEGORY = "linear"        # "linear" (фьючерсы USDT), "inverse" или "spot"

# Если запросы блокируются Bybit из-за IP (403 "from the usa"), впиши прокси сюда,
# например "http://user:pass@1.2.3.4:8080". Оставь "" если прокси не нужен.
PROXY_URL = ""

SYNC_INTERVAL_SEC = 300          # как часто обновлять базу (в секундах), 300 = 5 мин
INITIAL_BACKFILL_DAYS = 180      # сколько дней истории подтянуть при первом запуске
DB_PATH = "trades.db"            # файл базы данных

# Мгновенные уведомления в Telegram при закрытии новой сделки (нужен ALLOWED_CHAT_ID)
NOTIFY_ON_NEW_TRADE = True

# Автоматический ежедневный отчёт со статистикой за сутки (нужен ALLOWED_CHAT_ID)
ENABLE_DAILY_REPORT = True
DAILY_REPORT_HOUR_UTC = 6        # час отправки отчёта по UTC (6 = 9:00 МСК)

# Автоматический еженедельный отчёт (итоги недели)
ENABLE_WEEKLY_REPORT = True
WEEKLY_REPORT_WEEKDAY = 6        # 0 = понедельник ... 6 = воскресенье
WEEKLY_REPORT_HOUR_UTC = 18

# Риск-алерт: если суммарный убыток за текущие сутки (по МСК/UTC) превышает это
# значение в USDT — бот сразу пришлёт предупреждение. 0 — выключить.
MAX_DAILY_LOSS_ALERT = 10

# Сколько синхронизаций подряд должны провалиться, чтобы бот прислал алерт
# "проблема с подключением к Bybit" (например, из-за блокировки IP или сбоя сети)
CONSECUTIVE_ERROR_THRESHOLD = 3
