# Bybit Stats Bot

Telegram-бот, который сам ведёт учёт твоих закрытых сделок с Bybit: периодически
сохраняет их в базу (SQLite) и по команде показывает статистику (PnL, win rate,
лучшая/худшая сделка и т.д.).

Все настройки вписываются прямо в код — переменные окружения не нужны.

## 1. Получи данные

**Telegram Bot Token:**
1. Напиши в Telegram боту [@BotFather](https://t.me/BotFather)
2. `/newbot` → следуй инструкциям → скопируй токен вида `123456:ABC-DEF...`

**Bybit API ключи:**
1. Bybit → Аккаунт → API Management → Create New Key
2. Права доступа: только **Read-Only** (учёт не требует торговых прав!)
3. Скопируй `API Key` и `API Secret`

## 2. Впиши настройки в bot.py

Открой файл `bot.py`, в самом начале найди блок:

```python
TELEGRAM_TOKEN = "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER"
ALLOWED_CHAT_ID = ""
BYBIT_API_KEY = "ВСТАВЬ_СЮДА_BYBIT_API_KEY"
BYBIT_API_SECRET = "ВСТАВЬ_СЮДА_BYBIT_API_SECRET"
```

Замени на свои значения, например:

```python
TELEGRAM_TOKEN = "7123456789:AAHf3kd9dKfj39dkfjKDFJfj39dk"
ALLOWED_CHAT_ID = ""
BYBIT_API_KEY = "aB1cD2eF3gH4iJ5k"
BYBIT_API_SECRET = "zY9xW8vU7tS6rQ5pO4nM3lK2jI1hG0f"
```

`ALLOWED_CHAT_ID` пока оставь пустым — впишешь позже (см. шаг 4).

## 3. Залей код на GitHub

1. Создай новый репозиторий на GitHub (например `bybit-stats-bot`)
2. Загрузи туда все файлы из этой папки (`bot.py`, `db.py`, `requirements.txt`, `Procfile`)

Через веб-интерфейс GitHub: "Add file" → "Upload files" → перетащи файлы → Commit.

## 4. Разверни на Railway

1. Зайди на [railway.app](https://railway.app) → New Project → **Deploy from GitHub repo**
2. Выбери свой репозиторий
3. Railway сам определит `Procfile` и `requirements.txt` и задеплоит — никаких Variables
   заполнять не нужно
4. В логах должно появиться `Бот запущен. Ожидаю команды...`

**Важно:** без Railway Volume база сделок будет обнуляться при каждом передеплое.
Если хочешь постоянное хранилище: Project → Settings → Volumes → Add Volume,
путь монтирования `/app/data`, и в `bot.py` поменяй `DB_PATH = "trades.db"` на
`DB_PATH = "/app/data/trades.db"`.

## 5. Ограничь бота только собой (рекомендуется)

1. Напиши боту в Telegram `/start` — он пришлёт твой `chat_id`
2. Впиши его в `bot.py`: `ALLOWED_CHAT_ID = "123456789"`
3. Закоммить изменение на GitHub — Railway автоматически передеплоит бота с новым кодом

Без этого шага бот будет отвечать любому, кто найдёт его в Telegram.

## 6. Пользуйся

- `/start` — приветствие и твой chat_id
- `/stats_today` — статистика за сегодня
- `/stats_week` — за 7 дней
- `/stats_month` — за 30 дней
- `/stats_all` — за всё время, что бот ведёт учёт
- `/balance` — текущий баланс аккаунта
- `/sync` — принудительно обновить базу прямо сейчас

## Как это работает

- Фоновый поток каждые `SYNC_INTERVAL_SEC` секунд (по умолчанию 300 = 5 мин) запрашивает
  у Bybit новые закрытые сделки и сохраняет их в SQLite, без дублей.
- Все команды `/stats_*` считают статистику по локальной базе, а не напрямую по API —
  это быстрее и позволяет хранить историю дольше, чем даёт Bybit.

## ⚠️ Важно про безопасность

Ключи Bybit и токен бота теперь лежат прямо в коде. Если репозиторий на GitHub
публичный — их увидит кто угодно. Сделай репозиторий **приватным**:
Settings репозитория → Danger Zone → Change visibility → Private.
