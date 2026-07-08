import os
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import telebot
from pybit.unified_trading import HTTP

import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bybit-stats-bot")

# ============================================================================
# ===================  НАСТРОЙКИ — ВПИШИ СВОИ ЗНАЧЕНИЯ СЮДА  ===============
# ============================================================================

TELEGRAM_TOKEN = "8911982147:AAGemvrrQxN-HkL5N3ZuAFtRI5ruPH3IBI8"

# Если оставить пустой строкой "" — бот будет отвечать в любом чате.
# После первого /start бот пришлёт твой chat_id, впиши его сюда и задеплой заново.
ALLOWED_CHAT_ID = "6716942872"

BYBIT_API_KEY = "pjhnpKwa69BZaLroXz"
BYBIT_API_SECRET = "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"

BYBIT_TESTNET = False            # True — тестовая сеть Bybit, False — реальная
BYBIT_CATEGORY = "linear"        # "linear" (фьючерсы USDT), "inverse" или "spot"

SYNC_INTERVAL_SEC = 300          # как часто обновлять базу (в секундах), 300 = 5 мин
INITIAL_BACKFILL_DAYS = 180      # сколько дней истории подтянуть при первом запуске
DB_PATH = "trades.db"            # файл базы данных

# ============================================================================

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

# ---------------------- Синхронизация сделок с Bybit в локальную базу ----------------------

def fetch_closed_pnl(start_ms: int, end_ms: int):
    """Тянет закрытые сделки Bybit за период. Bybit ограничивает окно 7 днями за один запрос."""
    all_trades = []
    window = 7 * 24 * 60 * 60 * 1000
    cur_start = start_ms
    while cur_start < end_ms:
        cur_end = min(cur_start + window, end_ms)
        cursor = None
        while True:
            params = dict(category=BYBIT_CATEGORY, startTime=cur_start, endTime=cur_end, limit=100)
            if cursor:
                params["cursor"] = cursor
            resp = session.get_closed_pnl(**params)
            result = resp.get("result", {})
            all_trades.extend(result.get("list", []))
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
            time.sleep(0.1)
        cur_start = cur_end
    return all_trades


def sync_trades():
    """Забирает новые сделки с момента последней синхронизации и сохраняет в БД."""
    last_sync = db.get_last_sync_time()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if last_sync == 0:
        start_ms = now_ms - INITIAL_BACKFILL_DAYS * 24 * 60 * 60 * 1000
        log.info("Первый запуск: загружаю историю за %s дней", INITIAL_BACKFILL_DAYS)
    else:
        start_ms = last_sync

    trades = fetch_closed_pnl(start_ms, now_ms)
    saved = db.upsert_trades(trades)
    db.set_last_sync_time(now_ms)
    log.info("Синхронизация: получено %s сделок, новых записей: %s", len(trades), saved)
    return saved


def sync_loop():
    while True:
        try:
            sync_trades()
        except Exception:
            log.exception("Ошибка синхронизации")
        time.sleep(SYNC_INTERVAL_SEC)


# ---------------------- Построение отчётов из локальной базы ----------------------

def build_stats_message(period_name: str, days: int) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    trades = db.query_stats(int(start.timestamp() * 1000), int(end.timestamp() * 1000))

    if not trades:
        return f"📊 <b>Статистика ({period_name})</b>\n\nЗа этот период закрытых сделок не найдено."

    total_pnl = sum(t["closed_pnl"] for t in trades)
    wins = [t for t in trades if t["closed_pnl"] > 0]
    losses = [t for t in trades if t["closed_pnl"] < 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0
    avg_win = (sum(t["closed_pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["closed_pnl"] for t in losses) / len(losses)) if losses else 0
    best = max(trades, key=lambda t: t["closed_pnl"])
    worst = min(trades, key=lambda t: t["closed_pnl"])

    msg = (
        f"📊 <b>Статистика ({period_name})</b>\n\n"
        f"Всего сделок: <b>{len(trades)}</b>\n"
        f"Прибыльных: <b>{len(wins)}</b> | Убыточных: <b>{len(losses)}</b>\n"
        f"Win rate: <b>{win_rate:.1f}%</b>\n\n"
        f"Итоговый PnL: <b>{total_pnl:.2f} USDT</b>\n"
        f"Средняя прибыль: <b>{avg_win:.2f} USDT</b>\n"
        f"Средний убыток: <b>{avg_loss:.2f} USDT</b>\n\n"
        f"🏆 Лучшая сделка: {best['symbol']} ({best['closed_pnl']:.2f} USDT)\n"
        f"💀 Худшая сделка: {worst['symbol']} ({worst['closed_pnl']:.2f} USDT)"
    )
    return msg


def get_balance_message() -> str:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    result = resp.get("result", {}).get("list", [])
    if not result:
        return "Не удалось получить баланс."

    account = result[0]
    total_equity = account.get("totalEquity", "N/A")
    total_pnl_unrealized = account.get("totalPerpUPL", "N/A")

    lines = [f"💰 <b>Баланс аккаунта</b>\n", f"Общий эквити: <b>{float(total_equity):.2f} USDT</b>"]
    if total_pnl_unrealized not in ("N/A", None, ""):
        lines.append(f"Нереализованный PnL: <b>{float(total_pnl_unrealized):.2f} USDT</b>")

    return "\n".join(lines)


# ---------------------- Проверка доступа ----------------------

def is_allowed(message) -> bool:
    if ALLOWED_CHAT_ID is None:
        return True
    return str(message.chat.id) == str(ALLOWED_CHAT_ID)


# ---------------------- Хендлеры команд ----------------------

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if not is_allowed(message):
        return
    bot.reply_to(
        message,
        "Привет! Я бот для учёта сделок Bybit.\n"
        "Я сам сохраняю все твои закрытые сделки в базу и веду постоянную статистику.\n\n"
        "Команды:\n"
        "/stats_today — статистика за сегодня\n"
        "/stats_week — статистика за 7 дней\n"
        "/stats_month — статистика за 30 дней\n"
        "/stats_all — статистика за всё время учёта\n"
        "/balance — текущий баланс аккаунта\n"
        "/sync — принудительно обновить базу прямо сейчас\n\n"
        f"Твой chat_id: <code>{message.chat.id}</code>",
    )


def _reply_stats(message, period_name, days):
    if not is_allowed(message):
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        msg = build_stats_message(period_name, days)
    except Exception as e:
        msg = f"Ошибка при получении статистики: {e}"
        log.exception("stats error")
    bot.reply_to(message, msg)


@bot.message_handler(commands=["stats_today"])
def cmd_stats_today(message):
    _reply_stats(message, "сегодня", 1)


@bot.message_handler(commands=["stats_week"])
def cmd_stats_week(message):
    _reply_stats(message, "7 дней", 7)


@bot.message_handler(commands=["stats_month"])
def cmd_stats_month(message):
    _reply_stats(message, "30 дней", 30)


@bot.message_handler(commands=["stats_all"])
def cmd_stats_all(message):
    _reply_stats(message, "всё время", 3650)


@bot.message_handler(commands=["balance"])
def cmd_balance(message):
    if not is_allowed(message):
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        msg = get_balance_message()
    except Exception as e:
        msg = f"Ошибка при получении баланса: {e}"
        log.exception("balance error")
    bot.reply_to(message, msg)


@bot.message_handler(commands=["sync"])
def cmd_sync(message):
    if not is_allowed(message):
        return
    bot.reply_to(message, "🔄 Синхронизирую сделки с Bybit...")
    try:
        saved = sync_trades()
        total = db.total_trade_count()
        bot.reply_to(message, f"Готово. Новых сделок сохранено: {saved}. Всего в базе: {total}.")
    except Exception as e:
        bot.reply_to(message, f"Ошибка синхронизации: {e}")
        log.exception("manual sync error")


# ---------------------- Точка входа ----------------------

if __name__ == "__main__":
    db.DB_PATH = DB_PATH
    db.init_db()
    threading.Thread(target=sync_loop, daemon=True).start()

    log.info("Бот запущен. Ожидаю команды...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.exception("Бот упал, перезапуск через 5 секунд: %s", e)
            time.sleep(5)
