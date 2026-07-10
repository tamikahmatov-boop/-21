import io
import csv
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

# Все настройки лежат в config.py — правь данные там, не здесь.
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
NOTIFY_ON_NEW_TRADE = getattr(config, "NOTIFY_ON_NEW_TRADE", True)
ENABLE_DAILY_REPORT = getattr(config, "ENABLE_DAILY_REPORT", True)
DAILY_REPORT_HOUR_UTC = getattr(config, "DAILY_REPORT_HOUR_UTC", 6)
ENABLE_WEEKLY_REPORT = getattr(config, "ENABLE_WEEKLY_REPORT", True)
WEEKLY_REPORT_WEEKDAY = getattr(config, "WEEKLY_REPORT_WEEKDAY", 6)
WEEKLY_REPORT_HOUR_UTC = getattr(config, "WEEKLY_REPORT_HOUR_UTC", 18)
MAX_DAILY_LOSS_ALERT = getattr(config, "MAX_DAILY_LOSS_ALERT", 0)

START_TIME = datetime.now(timezone.utc)

# ---------------------- База данных (учёт сделок) ----------------------
# Оптимизация: держим одно постоянное соединение вместо open/close на каждый
# запрос — это заметно быстрее на больших объёмах и снижает нагрузку на диск.

_db_lock = threading.RLock()
_conn: sqlite3.Connection = None


def init_db():
    global _conn
    with _db_lock:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")   # быстрее и безопаснее при параллельных чтениях
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
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
        _conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_updated_time ON trades(updated_time)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        _conn.commit()


def get_meta(key: str, default=None):
    with _db_lock:
        row = _conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value):
    with _db_lock:
        _conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        _conn.commit()


def get_last_sync_time() -> int:
    return int(get_meta("last_sync_ms", 0))


def set_last_sync_time(ts_ms: int):
    set_meta("last_sync_ms", ts_ms)


def upsert_trades(trades: list) -> list:
    """Сохраняет сделки одной транзакцией, возвращает список РЕАЛЬНО новых записей."""
    if not trades:
        return []
    new_trades = []
    with _db_lock:
        for t in trades:
            trade_id = t.get("orderId") or t.get("execId") or f"{t.get('symbol')}_{t.get('updatedTime')}"
            try:
                cur = _conn.execute(
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
                if cur.rowcount > 0:
                    new_trades.append(t)
            except Exception:
                continue
        _conn.commit()
    return new_trades


def query_trades(start_ms: int, end_ms: int, symbol: str = None) -> list:
    with _db_lock:
        if symbol:
            rows = _conn.execute(
                "SELECT * FROM trades WHERE updated_time BETWEEN ? AND ? AND symbol = ? ORDER BY updated_time ASC",
                (start_ms, end_ms, symbol),
            ).fetchall()
        else:
            rows = _conn.execute(
                "SELECT * FROM trades WHERE updated_time BETWEEN ? AND ? ORDER BY updated_time ASC",
                (start_ms, end_ms),
            ).fetchall()
    return [dict(r) for r in rows]


def total_trade_count() -> int:
    with _db_lock:
        row = _conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()
    return row["c"] if row else 0


# ---------------------- Настройки, изменяемые кнопками (хранятся в БД поверх config.py) ----------------------

def get_setting(name: str, default):
    val = get_meta(f"setting_{name}", None)
    if val is None:
        return default
    if isinstance(default, bool):
        return val == "True"
    if isinstance(default, int):
        try:
            return int(val)
        except ValueError:
            return default
    return val


def set_setting(name: str, value):
    set_meta(f"setting_{name}", value)


def distinct_symbols() -> list:
    with _db_lock:
        rows = _conn.execute(
            "SELECT symbol, COUNT(*) AS c FROM trades GROUP BY symbol ORDER BY c DESC LIMIT 20"
        ).fetchall()
    return [r["symbol"] for r in rows]


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


def call_with_retry(func, *args, retries=3, base_delay=1.5, **kwargs):
    """Обёртка над вызовами Bybit API: повторяет запрос при временных сетевых сбоях."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < retries:
                delay = base_delay * attempt
                log.warning("Bybit API сбой (попытка %s/%s): %s. Повтор через %.1fс", attempt, retries, e, delay)
                time.sleep(delay)
    raise last_err


def notify_admin(text: str):
    """Безопасная отправка сообщения администратору — никогда не бросает исключение."""
    if not ALLOWED_CHAT_ID:
        return
    try:
        bot.send_message(ALLOWED_CHAT_ID, text)
    except Exception:
        log.exception("Не удалось отправить admin-уведомление")


# ---------------------- Мониторинг соединения с Bybit ----------------------

_connection_state = {"is_down": False, "consecutive_failures": 0}


def report_sync_result(success: bool, error: str = None):
    """Отслеживает подряд идущие ошибки синхронизации и шлёт алерт о проблемах с доступом к Bybit."""
    threshold = getattr(config, "CONSECUTIVE_ERROR_THRESHOLD", 3)
    if success:
        if _connection_state["is_down"]:
            _connection_state["is_down"] = False
            notify_admin("🟢 <b>Соединение с Bybit восстановлено.</b> Синхронизация снова работает штатно.")
        _connection_state["consecutive_failures"] = 0
    else:
        _connection_state["consecutive_failures"] += 1
        if _connection_state["consecutive_failures"] >= threshold and not _connection_state["is_down"]:
            _connection_state["is_down"] = True
            notify_admin(
                f"🔴 <b>Проблема с подключением к Bybit</b>\n\n"
                f"Последние {_connection_state['consecutive_failures']} попыток синхронизации завершились ошибкой:\n"
                f"<code>{error}</code>\n\n"
                f"Бот продолжит пытаться автоматически, дополнительно делать ничего не нужно."
            )


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
            resp = call_with_retry(session.get_closed_pnl, **params)
            result = resp.get("result", {})
            all_trades.extend(result.get("list", []))
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
            time.sleep(0.1)
        cur_start = cur_end
    return all_trades


def sync_trades(silent_notify=False) -> int:
    """Забирает новые сделки, сохраняет в БД, опционально шлёт уведомления о новых."""
    last_sync = get_last_sync_time()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    is_first_run = last_sync == 0

    if is_first_run:
        start_ms = now_ms - INITIAL_BACKFILL_DAYS * 24 * 60 * 60 * 1000
        log.info("Первый запуск: загружаю историю за %s дней", INITIAL_BACKFILL_DAYS)
    else:
        start_ms = last_sync

    trades = fetch_closed_pnl(start_ms, now_ms)
    new_trades = upsert_trades(trades)
    set_last_sync_time(now_ms)
    log.info("Синхронизация: получено %s сделок, новых записей: %s", len(trades), len(new_trades))

    if get_setting("notify_new_trade", NOTIFY_ON_NEW_TRADE) and not silent_notify and not is_first_run and new_trades and ALLOWED_CHAT_ID:
        for t in new_trades:
            notify_new_trade(t)

    loss_limit = get_setting("max_daily_loss_alert", MAX_DAILY_LOSS_ALERT)
    if not is_first_run and loss_limit > 0 and ALLOWED_CHAT_ID:
        check_daily_loss_alert(loss_limit)

    return len(new_trades)


def check_daily_loss_alert(loss_limit: int):
    """Если суммарный убыток за текущие сутки (UTC) превышает лимит — шлём предупреждение один раз в сутки."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    already_alerted = get_meta("loss_alert_date", "")
    if already_alerted == today_str:
        return

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    trades_today = query_trades(int(day_start.timestamp() * 1000), int(datetime.now(timezone.utc).timestamp() * 1000))
    pnl_today = sum(t["closed_pnl"] for t in trades_today)

    if pnl_today <= -abs(loss_limit):
        try:
            bot.send_message(
                ALLOWED_CHAT_ID,
                f"⚠️ <b>Риск-алерт</b>\n\nУбыток за сегодня составил <b>{pnl_today:.2f} USDT</b>, "
                f"это превышает установленный лимит ({loss_limit} USDT).\n"
                f"Возможно, стоит сделать паузу в торговле.",
            )
            set_meta("loss_alert_date", today_str)
        except Exception:
            log.exception("Не удалось отправить риск-алерт")


def notify_new_trade(t: dict):
    try:
        pnl = float(t.get("closedPnl", 0) or 0)
        emoji = "✅" if pnl >= 0 else "❌"
        symbol = t.get("symbol")
        side = "Long" if t.get("side") == "Sell" else "Short"  # side в closed-pnl — сторона ЗАКРЫТИЯ
        qty = t.get("qty")
        msg = (
            f"{emoji} <b>Сделка закрыта: {symbol}</b>\n"
            f"Направление: {side} | Объём: {qty}\n"
            f"PnL: <b>{pnl:.2f} USDT</b>"
        )
        bot.send_message(ALLOWED_CHAT_ID, msg)
    except Exception:
        log.exception("Не удалось отправить уведомление о новой сделке")


def sync_loop():
    while True:
        try:
            sync_trades()
            report_sync_result(True)
        except Exception as e:
            log.error("Ошибка синхронизации: %s", str(e))
            report_sync_result(False, str(e))
        time.sleep(get_setting("sync_interval_sec", SYNC_INTERVAL_SEC))


def daily_report_loop():
    """Раз в сутки в заданный час отправляет статистику за прошедшие сутки."""
    if not ALLOWED_CHAT_ID:
        return
    last_sent_date = get_meta("last_daily_report_date", "")
    while True:
        if get_setting("enable_daily_report", ENABLE_DAILY_REPORT):
            now = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")
            if now.hour == DAILY_REPORT_HOUR_UTC and today_str != last_sent_date:
                try:
                    msg = "🗓 <b>Ежедневный отчёт</b>\n\n" + build_stats_message("сутки", 1)
                    bot.send_message(ALLOWED_CHAT_ID, msg)
                    set_meta("last_daily_report_date", today_str)
                    last_sent_date = today_str
                except Exception:
                    log.exception("Ошибка отправки ежедневного отчёта")
        time.sleep(60)


def weekly_report_loop():
    """Раз в неделю в заданный день/час отправляет итоги недели."""
    if not ALLOWED_CHAT_ID:
        return
    last_sent_week = get_meta("last_weekly_report_week", "")
    while True:
        if get_setting("enable_weekly_report", ENABLE_WEEKLY_REPORT):
            now = datetime.now(timezone.utc)
            week_key = now.strftime("%G-W%V")
            if now.weekday() == WEEKLY_REPORT_WEEKDAY and now.hour == WEEKLY_REPORT_HOUR_UTC and week_key != last_sent_week:
                try:
                    msg = "📅 <b>Итоги недели</b>\n\n" + build_stats_message("7 дней", 7)
                    bot.send_message(ALLOWED_CHAT_ID, msg)
                    set_meta("last_weekly_report_week", week_key)
                    last_sent_week = week_key
                except Exception:
                    log.exception("Ошибка отправки еженедельного отчёта")
        time.sleep(60)


# ---------------------- Открытые позиции и баланс ----------------------

def get_open_positions() -> list:
    resp = call_with_retry(session.get_positions, category=BYBIT_CATEGORY, settleCoin="USDT")
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
    resp = call_with_retry(session.get_wallet_balance, accountType="UNIFIED")
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

def compute_stats(trades: list, tz_offset: int = 0) -> dict:
    total_pnl = sum(t["closed_pnl"] for t in trades)
    wins = [t for t in trades if t["closed_pnl"] > 0]
    losses = [t for t in trades if t["closed_pnl"] < 0]
    breakeven = [t for t in trades if t["closed_pnl"] == 0]

    win_sum = sum(t["closed_pnl"] for t in wins)
    loss_sum = sum(t["closed_pnl"] for t in losses)  # отрицательное число

    win_rate = (len(wins) / len(trades) * 100) if trades else 0
    avg_win = (win_sum / len(wins)) if wins else 0
    avg_loss = (loss_sum / len(losses)) if losses else 0
    profit_factor = (win_sum / abs(loss_sum)) if loss_sum != 0 else (float("inf") if win_sum > 0 else 0)
    expectancy = (total_pnl / len(trades)) if trades else 0

    best = max(trades, key=lambda t: t["closed_pnl"]) if trades else None
    worst = min(trades, key=lambda t: t["closed_pnl"]) if trades else None

    # текущая серия + максимальные исторические серии побед/поражений
    cur_streak_type, cur_streak_len = None, 0
    for t in reversed(trades):
        pnl = t["closed_pnl"]
        cur_type = "win" if pnl > 0 else ("loss" if pnl < 0 else None)
        if cur_type is None:
            break
        if cur_streak_type is None:
            cur_streak_type = cur_type
            cur_streak_len = 1
        elif cur_type == cur_streak_type:
            cur_streak_len += 1
        else:
            break

    max_win_streak = max_loss_streak = run = 0
    run_type = None
    for t in trades:
        pnl = t["closed_pnl"]
        t_type = "win" if pnl > 0 else ("loss" if pnl < 0 else None)
        if t_type == run_type:
            run += 1
        else:
            run_type = t_type
            run = 1
        if t_type == "win":
            max_win_streak = max(max_win_streak, run)
        elif t_type == "loss":
            max_loss_streak = max(max_loss_streak, run)

    # максимальная просадка по накопительному PnL
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    equity_curve = []
    for t in trades:
        cum += t["closed_pnl"]
        equity_curve.append(cum)
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
        sym = t["symbol"]
        by_symbol.setdefault(sym, {"count": 0, "pnl": 0.0})
        by_symbol[sym]["count"] += 1
        by_symbol[sym]["pnl"] += t["closed_pnl"]
    top_symbols = sorted(by_symbol.items(), key=lambda kv: kv[1]["count"], reverse=True)[:5]

    # анализ по дням недели и часам (когда сделки прибыльнее/убыточнее), с учётом часового пояса
    weekday_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    by_weekday = {i: 0.0 for i in range(7)}
    by_hour = {i: 0.0 for i in range(24)}
    for t in trades:
        if t["updated_time"]:
            dt = datetime.fromtimestamp(t["updated_time"] / 1000, tz=timezone.utc) + timedelta(hours=tz_offset)
            by_weekday[dt.weekday()] += t["closed_pnl"]
            by_hour[dt.hour] += t["closed_pnl"]

    best_weekday = max(by_weekday.items(), key=lambda kv: kv[1]) if trades else None
    worst_weekday = min(by_weekday.items(), key=lambda kv: kv[1]) if trades else None
    best_hour = max(by_hour.items(), key=lambda kv: kv[1]) if trades else None
    worst_hour = min(by_hour.items(), key=lambda kv: kv[1]) if trades else None

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
        expectancy=expectancy,
        best=best,
        worst=worst,
        cur_streak_type=cur_streak_type,
        cur_streak_len=cur_streak_len,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        max_dd=max_dd,
        avg_duration_min=avg_duration_min,
        top_symbols=top_symbols,
        equity_curve=equity_curve,
        weekday_names=weekday_names,
        best_weekday=best_weekday,
        worst_weekday=worst_weekday,
        best_hour=best_hour,
        worst_hour=worst_hour,
    )


def _fmt_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f} мин"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f} ч"
    return f"{minutes / 60 / 24:.1f} дн"


def _fmt_delta(cur: float, prev: float, suffix: str = "") -> str:
    if prev == 0 and cur == 0:
        return ""
    diff = cur - prev
    arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "▬")
    return f" ({arrow}{abs(diff):.2f}{suffix} к пред. периоду)"


def build_stats_message(period_name: str, days: int, symbol: str = None) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    trades = query_trades(int(start.timestamp() * 1000), int(end.timestamp() * 1000), symbol=symbol)

    title = f"📊 <b>Статистика ({period_name}{f', {symbol}' if symbol else ''})</b>"

    if not trades:
        return f"{title}\n\nЗа этот период закрытых сделок не найдено."

    s = compute_stats(trades, tz_offset=get_setting("tz_offset", 0))

    # сравнение с предыдущим таким же периодом (только для коротких периодов, не 'всё время')
    compare_str = ""
    if days < 3000:
        prev_end = start
        prev_start = prev_end - timedelta(days=days)
        prev_trades = query_trades(int(prev_start.timestamp() * 1000), int(prev_end.timestamp() * 1000), symbol=symbol)
        if prev_trades:
            prev_pnl = sum(t["closed_pnl"] for t in prev_trades)
            compare_str = _fmt_delta(s["total_pnl"], prev_pnl, " USDT")

    pf_str = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    streak_emoji = "🔥" if s["cur_streak_type"] == "win" else "❄️"
    streak_str = (
        f"{streak_emoji} {s['cur_streak_len']} {'побед' if s['cur_streak_type'] == 'win' else 'поражений'} подряд"
        if s["cur_streak_type"] else "—"
    )

    lines = [
        f"{title}\n",
        f"Всего сделок: <b>{s['total']}</b>",
        f"Прибыльных: <b>{s['wins']}</b> | Убыточных: <b>{s['losses']}</b> | В ноль: {s['breakeven']}",
        f"Win rate: <b>{s['win_rate']:.1f}%</b>",
        f"Текущая серия: {streak_str}",
        f"Макс. серия побед/поражений: 🔥{s['max_win_streak']} / ❄️{s['max_loss_streak']}\n",
        f"Итоговый PnL: <b>{s['total_pnl']:.2f} USDT</b>{compare_str}",
        f"Profit factor: <b>{pf_str}</b>",
        f"Средняя прибыль: <b>{s['avg_win']:.2f} USDT</b>",
        f"Средний убыток: <b>{s['avg_loss']:.2f} USDT</b>",
        f"Ожидание на сделку (expectancy): <b>{s['expectancy']:.2f} USDT</b>",
        f"Макс. просадка (по закрытым): <b>{s['max_dd']:.2f} USDT</b>",
        f"Средняя длительность сделки: <b>{_fmt_duration(s['avg_duration_min'])}</b>\n",
        f"🏆 Лучшая: {s['best']['symbol']} ({s['best']['closed_pnl']:.2f} USDT)",
        f"💀 Худшая: {s['worst']['symbol']} ({s['worst']['closed_pnl']:.2f} USDT)",
    ]

    if not symbol and s["top_symbols"]:
        lines.append("\n📋 <b>По инструментам:</b>")
        for sym, data in s["top_symbols"]:
            emoji = "🟩" if data["pnl"] >= 0 else "🟥"
            lines.append(f"{emoji} {sym}: {data['count']} сделок, {data['pnl']:.2f} USDT")

    if days >= 7 and s["total"] >= 5:
        wd = s["weekday_names"]
        lines.append("\n🗓 <b>По времени (UTC):</b>")
        if s["best_weekday"] and s["best_weekday"][1] > 0:
            lines.append(f"Лучший день недели: {wd[s['best_weekday'][0]]} ({s['best_weekday'][1]:.2f} USDT)")
        if s["worst_weekday"] and s["worst_weekday"][1] < 0:
            lines.append(f"Худший день недели: {wd[s['worst_weekday'][0]]} ({s['worst_weekday'][1]:.2f} USDT)")
        if s["best_hour"] and s["best_hour"][1] > 0:
            lines.append(f"Лучший час: {s['best_hour'][0]}:00 ({s['best_hour'][1]:.2f} USDT)")
        if s["worst_hour"] and s["worst_hour"][1] < 0:
            lines.append(f"Худший час: {s['worst_hour'][0]}:00 ({s['worst_hour'][1]:.2f} USDT)")

    return "\n".join(lines)


# ---------------------- График капитала (equity curve) ----------------------

# ---------------------- Топ сделок ----------------------

def build_top_trades_message(days: int, top_n: int = 5) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    trades = query_trades(int(start.timestamp() * 1000), int(end.timestamp() * 1000))

    period_name = PERIOD_NAMES.get(days, f"{days} дней")
    if not trades:
        return f"🏆 <b>Топ сделок ({period_name})</b>\n\nЗа этот период закрытых сделок не найдено."

    best = sorted(trades, key=lambda t: t["closed_pnl"], reverse=True)[:top_n]
    worst = sorted(trades, key=lambda t: t["closed_pnl"])[:top_n]

    def fmt(t):
        dt = datetime.fromtimestamp(t["updated_time"] / 1000, tz=timezone.utc).strftime("%d.%m %H:%M") if t["updated_time"] else "—"
        return f"{t['symbol']} — <b>{t['closed_pnl']:.2f} USDT</b> ({dt})"

    lines = [f"🏆 <b>Топ сделок ({period_name})</b>\n", f"<b>Лучшие {len(best)}:</b>"]
    for i, t in enumerate(best, 1):
        lines.append(f"{i}. {fmt(t)}")
    lines.append(f"\n<b>Худшие {len(worst)}:</b>")
    for i, t in enumerate(worst, 1):
        lines.append(f"{i}. {fmt(t)}")

    return "\n".join(lines)


def build_equity_chart(days: int, symbol: str = None):
    """Строит PNG-график накопительного PnL. Возвращает BytesIO или None, если данных нет."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    trades = query_trades(int(start.timestamp() * 1000), int(end.timestamp() * 1000), symbol=symbol)
    if not trades:
        return None

    s = compute_stats(trades)
    curve = s["equity_curve"]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=140)
    color = "#2ecc71" if curve[-1] >= 0 else "#e74c3c"
    ax.plot(range(1, len(curve) + 1), curve, color=color, linewidth=2)
    ax.axhline(0, color="#888888", linewidth=0.8, linestyle="--")
    ax.fill_between(range(1, len(curve) + 1), curve, 0, color=color, alpha=0.15)
    ax.set_title(f"Кривая капитала{f' — {symbol}' if symbol else ''}", fontsize=13)
    ax.set_xlabel("Сделка №")
    ax.set_ylabel("Накопительный PnL, USDT")
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------- Экспорт в CSV ----------------------

def build_csv_export(days: int, symbol: str = None):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    trades = query_trades(int(start.timestamp() * 1000), int(end.timestamp() * 1000), symbol=symbol)
    if not trades:
        return None

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Символ", "Сторона", "Объём", "Цена входа", "Цена выхода", "PnL (USDT)", "Открыта", "Закрыта"])
    for t in trades:
        opened = datetime.fromtimestamp(t["created_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t["created_time"] else ""
        closed = datetime.fromtimestamp(t["updated_time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t["updated_time"] else ""
        writer.writerow([t["symbol"], t["side"], t["qty"], t["entry_price"], t["exit_price"], f"{t['closed_pnl']:.4f}", opened, closed])

    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM для корректного открытия в Excel
    data.seek(0)
    return data


# ---------------------- Статус бота ----------------------

# ---------------------- Проверка соединения ----------------------

def check_connection() -> str:
    """Пингует Bybit и измеряет задержку — удобно для диагностики проблем с доступом."""
    t0 = time.perf_counter()
    try:
        resp = call_with_retry(session.get_server_time, retries=1)
        latency_ms = (time.perf_counter() - t0) * 1000
        ret_code = resp.get("retCode")
        if ret_code == 0:
            return f"🟢 <b>Соединение с Bybit в порядке</b>\n\nЗадержка: {latency_ms:.0f} мс"
        return f"🟡 <b>Bybit ответил с кодом {ret_code}</b>\n\n{resp.get('retMsg', '')}"
    except Exception as e:
        return f"🔴 <b>Не удалось подключиться к Bybit</b>\n\n<code>{e}</code>"


def build_status_message() -> str:
    uptime = datetime.now(timezone.utc) - START_TIME
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes = rem // 60

    last_sync_ms = get_last_sync_time()
    last_sync_str = (
        datetime.fromtimestamp(last_sync_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if last_sync_ms else "ещё не было"
    )

    total = total_trade_count()

    notify = get_setting("notify_new_trade", NOTIFY_ON_NEW_TRADE)
    daily = get_setting("enable_daily_report", ENABLE_DAILY_REPORT)
    weekly = get_setting("enable_weekly_report", ENABLE_WEEKLY_REPORT)
    loss_limit = get_setting("max_daily_loss_alert", MAX_DAILY_LOSS_ALERT)
    sync_interval = get_setting("sync_interval_sec", SYNC_INTERVAL_SEC)
    category = get_setting("bybit_category", BYBIT_CATEGORY)

    lines = [
        "ℹ️ <b>Статус бота</b>\n",
        f"Аптайм: <b>{hours} ч {minutes} мин</b>",
        f"Сделок в базе: <b>{total}</b>",
        f"Последняя синхронизация: <b>{last_sync_str}</b>",
        f"Интервал синхронизации: {sync_interval} сек",
        f"Категория Bybit: {category} | Testnet: {BYBIT_TESTNET}",
        f"Уведомления о сделках: {'вкл' if notify else 'выкл'}",
        f"Ежедневный отчёт: {'вкл в ' + str(DAILY_REPORT_HOUR_UTC) + ':00 UTC' if daily else 'выкл'}",
        f"Еженедельный отчёт: {'вкл' if weekly else 'выкл'}",
        f"Риск-алерт по убытку: {(str(loss_limit) + ' USDT/сутки') if loss_limit > 0 else 'выкл'}",
    ]
    return "\n".join(lines)


# ---------------------- Проверка доступа ----------------------

def is_allowed(chat_id) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == str(ALLOWED_CHAT_ID)


# ---------------------- Меню (кнопки) ----------------------

PERIODS = [("Сегодня", 1), ("7 дней", 7), ("30 дней", 30), ("Всё время", 3650)]

# Текст кнопок обычной (не inline) клавиатуры — она всегда висит внизу экрана
BTN_STATS = "📊 Статистика"
BTN_POSITIONS = "📈 Открытые позиции"
BTN_CHART = "📉 График капитала"
BTN_SYMBOLS = "🔎 По инструменту"
BTN_TOP_TRADES = "🏆 Топ сделок"
BTN_EXPORT = "📤 Экспорт CSV"
BTN_BALANCE = "💰 Баланс"
BTN_SYNC = "🔄 Синхронизировать"
BTN_STATUS = "ℹ️ Статус"
BTN_CHECK_CONN = "🩺 Проверить соединение"
BTN_BACKUP = "💾 Резервная копия"
BTN_SETTINGS = "⚙️ Настройки"


def reply_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton(BTN_STATS), types.KeyboardButton(BTN_POSITIONS))
    kb.add(types.KeyboardButton(BTN_CHART), types.KeyboardButton(BTN_SYMBOLS))
    kb.add(types.KeyboardButton(BTN_TOP_TRADES), types.KeyboardButton(BTN_EXPORT))
    kb.add(types.KeyboardButton(BTN_BALANCE), types.KeyboardButton(BTN_SYNC))
    kb.add(types.KeyboardButton(BTN_STATUS), types.KeyboardButton(BTN_CHECK_CONN))
    kb.add(types.KeyboardButton(BTN_BACKUP), types.KeyboardButton(BTN_SETTINGS))
    return kb


def main_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
        types.InlineKeyboardButton("📈 Открытые позиции", callback_data="open_positions"),
    )
    kb.add(
        types.InlineKeyboardButton("📉 График капитала", callback_data="menu_chart"),
        types.InlineKeyboardButton("🔎 По инструменту", callback_data="menu_symbols"),
    )
    kb.add(
        types.InlineKeyboardButton("📤 Экспорт CSV", callback_data="menu_export"),
        types.InlineKeyboardButton("💰 Баланс", callback_data="balance"),
    )
    kb.add(
        types.InlineKeyboardButton("🏆 Топ сделок", callback_data="menu_top"),
        types.InlineKeyboardButton("🩺 Проверить соединение", callback_data="check_connection"),
    )
    kb.add(
        types.InlineKeyboardButton("🔄 Синхронизировать", callback_data="sync"),
        types.InlineKeyboardButton("ℹ️ Статус", callback_data="status"),
    )
    kb.add(
        types.InlineKeyboardButton("💾 Резервная копия базы", callback_data="backup"),
        types.InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
    )
    return kb


def period_menu(prefix: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(name, callback_data=f"{prefix}_{days}") for name, days in PERIODS]
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return kb


def stats_result_menu(days: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(name, callback_data=f"stats_{d}") for name, d in PERIODS]
    kb.add(*buttons)
    kb.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data=f"stats_{days}"),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"),
    )
    return kb


def symbols_menu() -> types.InlineKeyboardMarkup:
    symbols = distinct_symbols()
    kb = types.InlineKeyboardMarkup(row_width=3)
    if symbols:
        buttons = [types.InlineKeyboardButton(sym, callback_data=f"symstat_{sym}") for sym in symbols]
        kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return kb


def back_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return kb


LOSS_ALERT_PRESETS = [0, 50, 100, 200, 500, 1000]
SYNC_INTERVAL_PRESETS = [60, 300, 600, 900, 1800]
CATEGORY_PRESETS = ["linear", "spot", "inverse"]
TIMEZONE_PRESETS = [0, 1, 2, 3, 4, 5, -5]  # смещение от UTC в часах; 3 = МСК


def settings_menu() -> types.InlineKeyboardMarkup:
    notify = get_setting("notify_new_trade", NOTIFY_ON_NEW_TRADE)
    daily = get_setting("enable_daily_report", ENABLE_DAILY_REPORT)
    weekly = get_setting("enable_weekly_report", ENABLE_WEEKLY_REPORT)
    loss_limit = get_setting("max_daily_loss_alert", MAX_DAILY_LOSS_ALERT)
    sync_interval = get_setting("sync_interval_sec", SYNC_INTERVAL_SEC)
    category = get_setting("bybit_category", BYBIT_CATEGORY)
    tz_offset = get_setting("tz_offset", 0)

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        f"🔔 Уведомления о сделках: {'Вкл ✅' if notify else 'Выкл ⛔'}", callback_data="toggle_notify"
    ))
    kb.add(types.InlineKeyboardButton(
        f"🗓 Ежедневный отчёт: {'Вкл ✅' if daily else 'Выкл ⛔'}", callback_data="toggle_daily"
    ))
    kb.add(types.InlineKeyboardButton(
        f"📅 Еженедельный отчёт: {'Вкл ✅' if weekly else 'Выкл ⛔'}", callback_data="toggle_weekly"
    ))
    kb.add(types.InlineKeyboardButton(
        f"⚠️ Риск-алерт: {(str(loss_limit) + ' USDT/сутки') if loss_limit > 0 else 'Выкл'}", callback_data="cycle_loss_alert"
    ))
    kb.add(types.InlineKeyboardButton(
        f"⏱ Интервал синхронизации: {sync_interval // 60} мин", callback_data="cycle_sync_interval"
    ))
    kb.add(types.InlineKeyboardButton(
        f"📂 Категория Bybit: {category}", callback_data="cycle_category"
    ))
    kb.add(types.InlineKeyboardButton(
        f"🌍 Часовой пояс (для «По времени»): UTC{tz_offset:+d}", callback_data="cycle_timezone"
    ))
    kb.add(types.InlineKeyboardButton("🔔 Отправить тестовое уведомление", callback_data="test_notify"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return kb


PERIOD_NAMES = {1: "сегодня", 7: "7 дней", 30: "30 дней", 3650: "всё время"}


# ---------------------- Хендлеры ----------------------

@bot.message_handler(commands=["start", "help", "menu"])
def cmd_start(message):
    if not is_allowed(message.chat.id):
        return
    bot.send_message(
        message.chat.id,
        "Привет! Я бот для учёта сделок Bybit.\n"
        "Сохраняю закрытые сделки, слежу за открытыми позициями и присылаю уведомления.\n\n"
        f"Твой chat_id: <code>{message.chat.id}</code>\n\n"
        "Кнопки снизу — быстрый доступ. Кнопки в сообщении — подробное меню.",
        reply_markup=reply_keyboard(),
    )
    bot.send_message(message.chat.id, "Выбери, что показать:", reply_markup=main_menu())


@bot.message_handler(func=lambda m: m.text in {
    BTN_STATS, BTN_POSITIONS, BTN_CHART, BTN_SYMBOLS, BTN_TOP_TRADES, BTN_EXPORT,
    BTN_BALANCE, BTN_SYNC, BTN_STATUS, BTN_CHECK_CONN, BTN_BACKUP, BTN_SETTINGS,
})
def handle_reply_keyboard(message):
    """Обрабатывает нажатия обычной (нижней) клавиатуры — присылает то же меню/данные, что и inline-кнопки."""
    if not is_allowed(message.chat.id):
        return
    chat_id = message.chat.id
    text = message.text

    if text == BTN_STATS:
        bot.send_message(chat_id, "За какой период показать статистику?", reply_markup=period_menu("stats"))
    elif text == BTN_POSITIONS:
        bot.send_message(chat_id, "Загружаю позиции...")
        bot.send_message(chat_id, build_open_positions_message(), reply_markup=back_menu())
    elif text == BTN_CHART:
        bot.send_message(chat_id, "За какой период построить график?", reply_markup=period_menu("chart"))
    elif text == BTN_SYMBOLS:
        bot.send_message(chat_id, "Выбери инструмент:", reply_markup=symbols_menu())
    elif text == BTN_TOP_TRADES:
        bot.send_message(chat_id, "За какой период показать топ сделок?", reply_markup=period_menu("top"))
    elif text == BTN_EXPORT:
        bot.send_message(chat_id, "За какой период выгрузить CSV?", reply_markup=period_menu("export"))
    elif text == BTN_BALANCE:
        bot.send_message(chat_id, get_balance_message(), reply_markup=back_menu())
    elif text == BTN_SYNC:
        saved = sync_trades(silent_notify=True)
        total = total_trade_count()
        bot.send_message(chat_id, f"✅ Готово.\nНовых сделок сохранено: {saved}\nВсего в базе: {total}", reply_markup=back_menu())
    elif text == BTN_STATUS:
        bot.send_message(chat_id, build_status_message(), reply_markup=back_menu())
    elif text == BTN_CHECK_CONN:
        bot.send_message(chat_id, "Проверяю...")
        bot.send_message(chat_id, check_connection(), reply_markup=back_menu())
    elif text == BTN_BACKUP:
        try:
            with _db_lock:
                _conn.execute("PRAGMA wal_checkpoint(FULL)")
            with open(DB_PATH, "rb") as f:
                bot.send_document(chat_id, types.InputFile(f, filename="trades_backup.db"), reply_markup=back_menu())
        except FileNotFoundError:
            bot.send_message(chat_id, "База данных пока пуста.", reply_markup=back_menu())
    elif text == BTN_SETTINGS:
        bot.send_message(chat_id, "⚙️ <b>Настройки бота</b>", reply_markup=settings_menu())


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global BYBIT_CATEGORY
    if not is_allowed(call.message.chat.id):
        return

    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    try:
        if data == "menu_main":
            bot.edit_message_text("Выбери, что показать:", chat_id, msg_id, reply_markup=main_menu())

        elif data == "menu_stats":
            bot.edit_message_text("За какой период показать статистику?", chat_id, msg_id, reply_markup=period_menu("stats"))

        elif data == "menu_chart":
            bot.edit_message_text("За какой период построить график?", chat_id, msg_id, reply_markup=period_menu("chart"))

        elif data == "menu_export":
            bot.edit_message_text("За какой период выгрузить CSV?", chat_id, msg_id, reply_markup=period_menu("export"))

        elif data == "menu_symbols":
            bot.edit_message_text("Выбери инструмент:", chat_id, msg_id, reply_markup=symbols_menu())

        elif data == "menu_top":
            bot.edit_message_text("За какой период показать топ сделок?", chat_id, msg_id, reply_markup=period_menu("top"))

        elif data.startswith("top_"):
            days = int(data.split("_")[1])
            bot.answer_callback_query(call.id, "Считаю...")
            msg = build_top_trades_message(days)
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=period_menu("top"))

        elif data.startswith("stats_"):
            days = int(data.split("_")[1])
            bot.answer_callback_query(call.id, "Считаю...")
            msg = build_stats_message(PERIOD_NAMES.get(days, f"{days} дней"), days)
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=stats_result_menu(days))

        elif data.startswith("symstat_"):
            symbol = data.split("_", 1)[1]
            bot.answer_callback_query(call.id, "Считаю...")
            msg = build_stats_message("всё время", 3650, symbol=symbol)
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=symbols_menu())

        elif data.startswith("chart_"):
            days = int(data.split("_")[1])
            bot.answer_callback_query(call.id, "Строю график...")
            chart = build_equity_chart(days)
            if chart is None:
                bot.send_message(chat_id, "За этот период данных нет.", reply_markup=period_menu("chart"))
            else:
                bot.send_photo(chat_id, chart, caption=f"📉 Кривая капитала ({PERIOD_NAMES.get(days, str(days)+' дней')})", reply_markup=period_menu("chart"))

        elif data.startswith("export_"):
            days = int(data.split("_")[1])
            bot.answer_callback_query(call.id, "Готовлю файл...")
            csv_data = build_csv_export(days)
            if csv_data is None:
                bot.send_message(chat_id, "За этот период данных нет.", reply_markup=period_menu("export"))
            else:
                fname = f"trades_{PERIOD_NAMES.get(days, str(days))}.csv".replace(" ", "_")
                bot.send_document(chat_id, types.InputFile(csv_data, filename=fname), reply_markup=period_menu("export"))

        elif data == "open_positions":
            bot.answer_callback_query(call.id, "Загружаю позиции...")
            msg = build_open_positions_message()
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=back_menu())

        elif data == "balance":
            bot.answer_callback_query(call.id, "Загружаю баланс...")
            msg = get_balance_message()
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=back_menu())

        elif data == "status":
            bot.answer_callback_query(call.id)
            msg = build_status_message()
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=back_menu())

        elif data == "check_connection":
            bot.answer_callback_query(call.id, "Проверяю...")
            msg = check_connection()
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=back_menu())

        elif data == "backup":
            bot.answer_callback_query(call.id, "Готовлю файл базы...")
            try:
                with _db_lock:
                    _conn.execute("PRAGMA wal_checkpoint(FULL)")
                with open(DB_PATH, "rb") as f:
                    bot.send_document(chat_id, types.InputFile(f, filename="trades_backup.db"), reply_markup=back_menu())
            except FileNotFoundError:
                bot.send_message(chat_id, "База данных пока пуста.", reply_markup=back_menu())

        elif data == "menu_settings":
            bot.answer_callback_query(call.id)
            bot.edit_message_text("⚙️ <b>Настройки бота</b>\n\nНажимай, чтобы менять — сохраняется сразу, без передеплоя.", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "toggle_notify":
            cur = get_setting("notify_new_trade", NOTIFY_ON_NEW_TRADE)
            set_setting("notify_new_trade", not cur)
            bot.answer_callback_query(call.id, "Готово")
            bot.edit_message_text("⚙️ <b>Настройки бота</b>", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "toggle_daily":
            cur = get_setting("enable_daily_report", ENABLE_DAILY_REPORT)
            set_setting("enable_daily_report", not cur)
            bot.answer_callback_query(call.id, "Готово")
            bot.edit_message_text("⚙️ <b>Настройки бота</b>", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "toggle_weekly":
            cur = get_setting("enable_weekly_report", ENABLE_WEEKLY_REPORT)
            set_setting("enable_weekly_report", not cur)
            bot.answer_callback_query(call.id, "Готово")
            bot.edit_message_text("⚙️ <b>Настройки бота</b>", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "cycle_loss_alert":
            cur = get_setting("max_daily_loss_alert", MAX_DAILY_LOSS_ALERT)
            idx = LOSS_ALERT_PRESETS.index(cur) if cur in LOSS_ALERT_PRESETS else 0
            new_val = LOSS_ALERT_PRESETS[(idx + 1) % len(LOSS_ALERT_PRESETS)]
            set_setting("max_daily_loss_alert", new_val)
            bot.answer_callback_query(call.id, f"Риск-алерт: {new_val if new_val else 'выключен'}")
            bot.edit_message_text("⚙️ <b>Настройки бота</b>", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "cycle_sync_interval":
            cur = get_setting("sync_interval_sec", SYNC_INTERVAL_SEC)
            idx = SYNC_INTERVAL_PRESETS.index(cur) if cur in SYNC_INTERVAL_PRESETS else 0
            new_val = SYNC_INTERVAL_PRESETS[(idx + 1) % len(SYNC_INTERVAL_PRESETS)]
            set_setting("sync_interval_sec", new_val)
            bot.answer_callback_query(call.id, f"Интервал: {new_val // 60} мин")
            bot.edit_message_text("⚙️ <b>Настройки бота</b>", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "cycle_category":
            cur = get_setting("bybit_category", BYBIT_CATEGORY)
            idx = CATEGORY_PRESETS.index(cur) if cur in CATEGORY_PRESETS else 0
            new_val = CATEGORY_PRESETS[(idx + 1) % len(CATEGORY_PRESETS)]
            set_setting("bybit_category", new_val)
            BYBIT_CATEGORY = new_val
            bot.answer_callback_query(call.id, f"Категория: {new_val}")
            bot.edit_message_text("⚙️ <b>Настройки бота</b>", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "cycle_timezone":
            cur = get_setting("tz_offset", 0)
            idx = TIMEZONE_PRESETS.index(cur) if cur in TIMEZONE_PRESETS else 0
            new_val = TIMEZONE_PRESETS[(idx + 1) % len(TIMEZONE_PRESETS)]
            set_setting("tz_offset", new_val)
            bot.answer_callback_query(call.id, f"Часовой пояс: UTC{new_val:+d}")
            bot.edit_message_text("⚙️ <b>Настройки бота</b>", chat_id, msg_id, reply_markup=settings_menu())

        elif data == "test_notify":
            bot.answer_callback_query(call.id, "Отправляю...")
            notify_admin("🔔 Это тестовое уведомление. Если ты его видишь — уведомления работают исправно.")

        elif data == "sync":
            bot.answer_callback_query(call.id, "Синхронизирую...")
            saved = sync_trades(silent_notify=True)
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


# ---------------------- Супервизор: держит бота живым 24/7 ----------------------

def supervised_thread(target, name: str):
    """Оборачивает фоновую функцию: если она упадёт с исключением — залогирует,
    уведомит администратора и перезапустит через паузу, вместо того чтобы тихо
    погибнуть и оставить бота без синхронизации/отчётов."""

    def wrapper():
        while True:
            try:
                target()
                break  # функция сама решила завершиться штатно (например, ALLOWED_CHAT_ID пуст)
            except Exception as e:
                log.exception("Поток '%s' упал", name)
                notify_admin(
                    f"⚠️ <b>Внутренний сбой</b>\n\n"
                    f"Фоновый процесс «{name}» упал с ошибкой и будет перезапущен через 15 секунд:\n"
                    f"<code>{e}</code>"
                )
                time.sleep(15)

    t = threading.Thread(target=wrapper, name=name, daemon=True)
    t.start()
    return t


def get_external_ip() -> str:
    try:
        import requests
        return requests.get("https://api.ipify.org", timeout=5).text
    except Exception as e:
        log.warning("Не удалось определить внешний IP: %s", e)
        return "неизвестен"


def run_bot_forever():
    """Главный цикл long-polling с автоперезапуском при любом сбое и защитой
    от бесконечного цикла быстрых рестартов (экспоненциальный backoff)."""
    restart_count = 0
    max_delay = 300  # не ждать больше 5 минут между попытками

    while True:
        try:
            if restart_count > 0:
                notify_admin(f"✅ <b>Бот перезапущен</b> (попытка №{restart_count + 1}) и снова работает.")
            restart_count = 0  # сбрасываем счётчик после успешного (без исключений) периода работы
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
            break  # infinity_polling завершился штатно (например, по остановке процесса)
        except Exception as e:
            restart_count += 1
            delay = min(5 * restart_count, max_delay)
            log.exception("Бот упал (попытка №%s), перезапуск через %sс", restart_count, delay)
            notify_admin(
                f"🔴 <b>Бот упал и перезапускается</b>\n\n"
                f"Попытка №{restart_count}, повтор через {delay} сек:\n"
                f"<code>{e}</code>"
            )
            time.sleep(delay)


def setup_graceful_shutdown():
    """Ловим SIGTERM/SIGINT (Railway шлёт их при остановке/передеплое) и уведомляем об этом,
    чтобы отличать плановую остановку от настоящего краша."""
    import signal

    def handler(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info("Получен сигнал %s — бот останавливается (вероятно, плановый передеплой).", sig_name)
        notify_admin(f"🛑 <b>Бот остановлен</b> (сигнал {sig_name}) — обычно это плановый передеплой на Railway.")
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    except Exception:
        pass  # на некоторых платформах не все сигналы доступны — не критично


# ---------------------- Точка входа ----------------------

if __name__ == "__main__":
    setup_graceful_shutdown()

    log.info("Внешний IP этого сервера: %s", get_external_ip())

    init_db()
    BYBIT_CATEGORY = get_setting("bybit_category", BYBIT_CATEGORY)

    supervised_thread(sync_loop, "Синхронизация сделок")
    supervised_thread(daily_report_loop, "Ежедневный отчёт")
    supervised_thread(weekly_report_loop, "Еженедельный отчёт")

    log.info("Бот запущен. Ожидаю команды...")
    notify_admin("✅ <b>Бот запущен</b> и готов к работе 24/7.")

    run_bot_forever()
