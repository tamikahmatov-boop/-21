import time
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types
from pybit.unified_trading import HTTP

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bybit-stats-bot")

# Всё, что было настройками, теперь лежит в config.py — правь данные там.
TELEGRAM_TOKEN = config.TELEGRAM_TOKEN
ALLOWED_CHAT_ID = config.ALLOWED_CHAT_ID
BYBIT_API_KEY = config.BYBIT_API_KEY
BYBIT_API_SECRET = config.BYBIT_API_SECRET
BYBIT_TESTNET = config.BYBIT_TESTNET
BYBIT_CATEGORY = config.BYBIT_CATEGORY
PROXY_URL = config.PROXY_URL
SYNC_INTERVAL_SEC = config.SYNC_INTERVAL_SEC
INITIAL_BACKFILL_DAYS = config.INITIAL_BACKFILL_DAYS
DB_PATH = config.DB_PATH


# ---------------------- База данных (учёт сделок) ----------------------

_db_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock:
        conn = get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                qty REAL,
                entry_price REAL,
                exit_price REAL,
                closed_pnl REAL,
                created_time INTEGER,
                updated_time INTEGER
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()


def get_last_sync_time() -> int:
    with _db_lock:
        conn = get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key = 'last_sync_ms'").fetchone()
        conn.close()
    return int(row["value"]) if row else 0


def set_last_sync_time(ts_ms: int):
    with _db_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_sync_ms', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(ts_ms),),
        )
        conn.commit()
        conn.close()


def upsert_trades(trades: list) -> int:
    if not trades:
        return 0
    with _db_lock:
        conn = get_conn()
        count = 0
        for t in trades:
            trade_id = t.get("orderId") or t.get("execId") or f"{t.get('symbol')}_{t.get('updatedTime')}"
            try:
                conn.execute(
                    """
                    INSERT INTO trades (id, symbol, side, qty, entry_price, exit_price, closed_pnl, created_time, updated_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        trade_id,
                        t.get("symbol"),
                        t.get("side"),
                        float(t.get("qty", 0) or 0),
                        float(t.get("avgEntryPrice", 0) or 0),
                        float(t.get("avgExitPrice", 0) or 0),
                        float(t.get("closedPnl", 0) or 0),
                        int(t.get("createdTime", 0) or 0),
                        int(t.get("updatedTime", 0) or 0),
                    ),
                )
                count += 1
            except Exception:
                continue
        conn.commit()
        conn.close()
    return count


def query_trades(start_ms: int, end_ms: int) -> list:
    with _db_lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM trades WHERE updated_time BETWEEN ? AND ? ORDER BY updated_time ASC",
            (start_ms, end_ms),
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def total_trade_count() -> int:
    with _db_lock:
        conn = get_conn()
        row = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()
        conn.close()
    return row["c"] if row else 0


# ---------------------- Telegram и Bybit ----------------------

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

session = HTTP(
    testnet=BYBIT_TESTNET,
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
    recv_window=20000,
)

if PROXY_URL:
    session.client.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    log.info("Использую прокси для запросов к Bybit")


# ---------------------- Синхронизация закрытых сделок ----------------------

def fetch_closed_pnl(start_ms: int, end_ms: int) -> list:
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


def sync_trades() -> int:
    last_sync = get_last_sync_time()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    if last_sync == 0:
        start_ms = now_ms - INITIAL_BACKFILL_DAYS * 24 * 60 * 60 * 1000
        log.info("Первый запуск: загружаю историю за %s дней", INITIAL_BACKFILL_DAYS)
    else:
        start_ms = last_sync

    trades = fetch_closed_pnl(start_ms, now_ms)
    saved = upsert_trades(trades)
    set_last_sync_time(now_ms)
    log.info("Синхронизация: получено %s сделок, новых записей: %s", len(trades), saved)
    return saved


def sync_loop():
    while True:
        try:
            sync_trades()
        except Exception as e:
            log.error("Ошибка синхронизации: %s", str(e))
        time.sleep(SYNC_INTERVAL_SEC)


# ---------------------- Открытые позиции и баланс ----------------------

def get_open_positions() -> list:
    resp = session.get_positions(category=BYBIT_CATEGORY, settleCoin="USDT")
    result = resp.get("result", {}).get("list", [])
    return [p for p in result if float(p.get("size", 0) or 0) > 0]


def build_open_positions_message() -> str:
    positions = get_open_positions()
    if not positions:
        return "📭 <b>Открытых позиций нет.</b>"

    lines = [f"📈 <b>Открытые позиции ({len(positions)})</b>\n"]
    total_upl = 0.0
    for p in positions:
        symbol = p.get("symbol")
        side = "🟢 Long" if p.get("side") == "Buy" else "🔴 Short"
        size = p.get("size")
        entry = float(p.get("avgPrice", 0) or 0)
        mark = float(p.get("markPrice", 0) or 0)
        upl = float(p.get("unrealisedPnl", 0) or 0)
        leverage = p.get("leverage", "?")
        total_upl += upl

        pct = ((mark - entry) / entry * 100) if entry else 0
        if p.get("side") == "Sell":
            pct = -pct

        emoji = "🟩" if upl >= 0 else "🟥"
        lines.append(
            f"{emoji} <b>{symbol}</b> {side} x{leverage}\n"
            f"   Объём: {size} | Вход: {entry:g} | Маркет: {mark:g}\n"
            f"   PnL: <b>{upl:.2f} USDT</b> ({pct:+.2f}%)\n"
        )

    lines.append(f"\n💵 <b>Суммарный нереализованный PnL: {total_upl:.2f} USDT</b>")
    return "\n".join(lines)


def get_balance_message() -> str:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    result = resp.get("result", {}).get("list", [])
    if not result:
        return "Не удалось получить баланс."

    account = result[0]
    total_equity = account.get("totalEquity", "N/A")
    total_pnl_unrealized = account.get("totalPerpUPL", "N/A")
    available = account.get("totalAvailableBalance", "N/A")

    lines = ["💰 <b>Баланс аккаунта</b>\n"]
    lines.append(f"Общий эквити: <b>{float(total_equity):.2f} USDT</b>")
    if available not in ("N/A", None, ""):
        lines.append(f"Доступно: <b>{float(available):.2f} USDT</b>")
    if total_pnl_unrealized not in ("N/A", None, ""):
        lines.append(f"Нереализованный PnL: <b>{float(total_pnl_unrealized):.2f} USDT</b>")

    return "\n".join(lines)


# ---------------------- Расширенная статистика по закрытым сделкам ----------------------

def compute_stats(trades: list) -> dict:
    total_pnl = sum(t["closed_pnl"] for t in trades)
    wins = [t for t in trades if t["closed_pnl"] > 0]
    losses = [t for t in trades if t["closed_pnl"] < 0]
    breakeven = [t for t in trades if t["closed_pnl"] == 0]

    win_sum = sum(t["closed_pnl"] for t in wins)
    loss_sum = sum(t["closed_pnl"] for t in losses)  # отрицательное число

    win_rate = (len(wins) / len(trades) * 100) if trades else 0
    avg_win = (win_sum / len(wins)) if wins else 0
    avg_loss = (loss_sum / len(losses)) if losses else 0
    profit_factor = (win_sum / abs(loss_sum)) if loss_sum != 0 else float("inf") if win_sum > 0 else 0

    best = max(trades, key=lambda t: t["closed_pnl"]) if trades else None
    worst = min(trades, key=lambda t: t["closed_pnl"]) if trades else None

    # текущая серия побед/поражений (по последним сделкам в хронологическом порядке)
    streak_type, streak_len = None, 0
    for t in reversed(trades):
        pnl = t["closed_pnl"]
        cur_type = "win" if pnl > 0 else ("loss" if pnl < 0 else None)
        if cur_type is None:
            break
        if streak_type is None:
            streak_type = cur_type
            streak_len = 1
        elif cur_type == streak_type:
            streak_len += 1
        else:
            break

    # максимальная просадка по накопительному PnL
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t["closed_pnl"]
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    # средняя длительность сделки
    durations = [
        (t["updated_time"] - t["created_time"]) / 1000 / 60
        for t in trades
        if t["updated_time"] and t["created_time"] and t["updated_time"] > t["created_time"]
    ]
    avg_duration_min = sum(durations) / len(durations) if durations else 0

    # разбивка по инструментам (топ-5 по количеству сделок)
    by_symbol = {}
    for t in trades:
        s = t["symbol"]
        by_symbol.setdefault(s, {"count": 0, "pnl": 0.0})
        by_symbol[s]["count"] += 1
        by_symbol[s]["pnl"] += t["closed_pnl"]
    top_symbols = sorted(by_symbol.items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]

    return dict(
        total=len(trades),
        total_pnl=total_pnl,
        wins=len(wins),
        losses=len(losses),
        breakeven=len(breakeven),
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        best=best,
        worst=worst,
        streak_type=streak_type,
        streak_len=streak_len,
        max_dd=max_dd,
        avg_duration_min=avg_duration_min,
        top_symbols=top_symbols,
    )


def build_stats_message(period_name: str, days: int) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    trades = query_trades(int(start.timestamp() * 1000), int(end.timestamp() * 1000))

    if not trades:
        return f"📊 <b>Статистика ({period_name})</b>\n\nЗа этот период закрытых сделок не найдено."

    s = compute_stats(trades)

    pf_str = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    streak_emoji = "🔥" if s["streak_type"] == "win" else "❄️"
    streak_str = f"{streak_emoji} {s['streak_len']} {'побед' if s['streak_type'] == 'win' else 'поражений'} подряд" if s["streak_type"] else "—"

    duration_str = (
        f"{s['avg_duration_min']:.0f} мин" if s["avg_duration_min"] < 180
        else f"{s['avg_duration_min']/60:.1f} ч"
    )

    lines = [
        f"📊 <b>Статистика ({period_name})</b>\n",
        f"Всего сделок: <b>{s['total']}</b>",
        f"Прибыльных: <b>{s['wins']}</b> | Убыточных: <b>{s['losses']}</b> | В ноль: {s['breakeven']}",
        f"Win rate: <b>{s['win_rate']:.1f}%</b>",
        f"Текущая серия: {streak_str}\n",
        f"Итоговый PnL: <b>{s['total_pnl']:.2f} USDT</b>",
        f"Profit factor: <b>{pf_str}</b>",
        f"Средняя прибыль: <b>{s['avg_win']:.2f} USDT</b>",
        f"Средний убыток: <b>{s['avg_loss']:.2f} USDT</b>",
        f"Макс. просадка (по закрытым): <b>{s['max_dd']:.2f} USDT</b>",
        f"Средняя длительность сделки: <b>{duration_str}</b>\n",
        f"🏆 Лучшая: {s['best']['symbol']} ({s['best']['closed_pnl']:.2f} USDT)",
        f"💀 Худшая: {s['worst']['symbol']} ({s['worst']['closed_pnl']:.2f} USDT)",
    ]

    if s["top_symbols"]:
        lines.append("\n📋 <b>По инструментам:</b>")
        for symbol, data in s["top_symbols"]:
            emoji = "🟩" if data["pnl"] >= 0 else "🟥"
            lines.append(f"{emoji} {symbol}: {data['count']} сделок, {data['pnl']:.2f} USDT")

    return "\n".join(lines)


# ---------------------- Проверка доступа ----------------------

def is_allowed(chat_id) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == str(ALLOWED_CHAT_ID)


# ---------------------- Меню (кнопки) ----------------------

def main_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
        types.InlineKeyboardButton("📈 Открытые позиции", callback_data="open_positions"),
    )
    kb.add(
        types.InlineKeyboardButton("💰 Баланс", callback_data="balance"),
        types.InlineKeyboardButton("🔄 Синхронизировать", callback_data="sync"),
    )
    return kb


def stats_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Сегодня", callback_data="stats_1"),
        types.InlineKeyboardButton("7 дней", callback_data="stats_7"),
    )
    kb.add(
        types.InlineKeyboardButton("30 дней", callback_data="stats_30"),
        types.InlineKeyboardButton("Всё время", callback_data="stats_3650"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return kb


def back_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return kb


# ---------------------- Хендлеры ----------------------

@bot.message_handler(commands=["start", "help", "menu"])
def cmd_start(message):
    if not is_allowed(message.chat.id):
        return
    bot.send_message(
        message.chat.id,
        "Привет! Я бот для учёта сделок Bybit.\n"
        "Я сам сохраняю закрытые сделки в базу и слежу за открытыми позициями.\n\n"
        f"Твой chat_id: <code>{message.chat.id}</code>\n\n"
        "Выбери, что показать:",
        reply_markup=main_menu(),
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not is_allowed(call.message.chat.id):
        return

    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    try:
        if data == "menu_main":
            bot.edit_message_text("Выбери, что показать:", chat_id, msg_id, reply_markup=main_menu())

        elif data == "menu_stats":
            bot.edit_message_text("За какой период показать статистику?", chat_id, msg_id, reply_markup=stats_menu())

        elif data.startswith("stats_"):
            days = int(data.split("_")[1])
            period_name = {1: "сегодня", 7: "7 дней", 30: "30 дней", 3650: "всё время"}.get(days, f"{days} дней")
            bot.answer_callback_query(call.id, "Считаю...")
            msg = build_stats_message(period_name, days)
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=stats_menu())

        elif data == "open_positions":
            bot.answer_callback_query(call.id, "Загружаю позиции...")
            msg = build_open_positions_message()
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=back_menu())

        elif data == "balance":
            bot.answer_callback_query(call.id, "Загружаю баланс...")
            msg = get_balance_message()
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=back_menu())

        elif data == "sync":
            bot.answer_callback_query(call.id, "Синхронизирую...")
            saved = sync_trades()
            total = total_trade_count()
            bot.edit_message_text(
                f"✅ Готово.\nНовых сделок сохранено: {saved}\nВсего в базе: {total}",
                chat_id, msg_id, reply_markup=back_menu(),
            )

        else:
            bot.answer_callback_query(call.id)

    except Exception as e:
        log.exception("Ошибка обработки кнопки")
        try:
            bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)
        except Exception:
            pass


# ---------------------- Точка входа ----------------------

if __name__ == "__main__":
    try:
        import requests
        my_ip = requests.get("https://api.ipify.org", timeout=5).text
        log.info("Внешний IP этого сервера: %s", my_ip)
    except Exception as e:
        log.warning("Не удалось определить внешний IP: %s", e)

    init_db()
    threading.Thread(target=sync_loop, daemon=True).start()

    log.info("Бот запущен. Ожидаю команды...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.exception("Бот упал, перезапуск через 5 секунд: %s", e)
            time.sleep(5)
