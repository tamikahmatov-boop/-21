# Bybit Stats Bot

Telegram-бот, который **сам ведёт учёт** твоих закрытых сделок с Bybit: периодически
сохраняет их в базу (SQLite) и по команде показывает статистику (PnL, win rate,
лучшая/худшая сделка и т.д.).

## 1. Получи данные

**Telegram Bot Token:**
1. Напиши в Telegram боту [@BotFather](https://t.me/BotFather)
2. `/newbot` → следуй инструкциям → скопируй токен вида `123456:ABC-DEF...`

**Bybit API ключи:**
1. Bybit → Аккаунт → API Management → Create New Key
2. Права доступа: только **Read-Only** (учёт не требует торговых прав!)
3. Скопируй `API Key` и `API Secret`

**Свой Telegram chat_id (необязательно, но рекомендуется):**
- Напиши боту после деплоя `/start` — он пришлёт твой chat_id, впиши его в переменную
  `TELEGRAM_CHAT_ID` в Railway, чтобы бот отвечал только тебе.

## 2. Залей код на GitHub

1. Создай новый репозиторий на GitHub (например `bybit-stats-bot`)
2. Загрузи туда все файлы из этой папки (`bot.py`, `db.py`, `requirements.txt`, `Procfile`)

Через веб-интерфейс GitHub: "Add file" → "Upload files" → перетащи файлы → Commit.

## 3. Разверни на Railway

1. Зайди на [railway.app](https://railway.app) → New Project → **Deploy from GitHub repo**
2. Выбери свой репозиторий
3. Railway сам определит `Procfile` и `requirements.txt`
4. Перейди в Variables и добавь переменные окружения:

| Переменная | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от BotFather |
| `TELEGRAM_CHAT_ID` | твой chat_id (можно добавить после первого `/start`) |
| `BYBIT_API_KEY` | ключ Bybit |
| `BYBIT_API_SECRET` | секрет Bybit |
| `BYBIT_CATEGORY` | `linear` (фьючерсы USDT), `inverse` или `spot` |
| `BYBIT_TESTNET` | `false` (или `true` для тестовой сети) |
| `SYNC_INTERVAL_SEC` | `300` (как часто обновлять базу, сек) |
| `INITIAL_BACKFILL_DAYS` | `180` (сколько истории подтянуть при первом запуске) |

5. **Важно:** чтобы база сделок не терялась при передеплое — добавь Railway Volume:
   Project → Settings → Volumes → Add Volume, путь монтирования `/app/data`,
   и добавь переменную `DB_PATH=/app/data/trades.db`. Без этого при каждом
   redeploy история будет собираться заново (не критично, но дольше).

6. Deploy. В логах должно появиться `Бот запущен. Ожидаю команды...`

## 4. Пользуйся

Напиши боту в Telegram:

- `/start` — приветствие и твой chat_id
- `/stats_today` — статистика за сегодня
- `/stats_week` — за 7 дней
- `/stats_month` — за 30 дней
- `/stats_all` — за всё время, что бот ведёт учёт
- `/balance` — текущий баланс аккаунта
- `/sync` — принудительно обновить базу прямо сейчас

## Как это работает

- Фоновый поток каждые `SYNC_INTERVAL_SEC` секунд запрашивает у Bybit новые закрытые
  сделки (`get_closed_pnl`) и сохраняет их в SQLite, без дублей.
- Все команды `/stats_*` считают статистику по локальной базе, а не напрямую по API —
  это быстрее и позволяет хранить историю дольше, чем даёт Bybit.
