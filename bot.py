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

START_TIME = datetime.now(timezone.utc)

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


def get_meta(key: str, default=None):
    with _db_lock:
        conn = get_conn()
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        conn.close()
    return row["value"] if row else default


def set_meta(key: str, value):
    with _db_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
        conn.close()


def get_last_sync_time() -> int:
    return int(get_meta("last_sync_ms", 0))


def set_last_sync_time(ts_ms: int):
    set_meta("last_sync_ms", ts_ms)


def upsert_trades(trades: list) -> list:
    """Сохраняет сделки, возвращает список РЕАЛЬНО новых записей (для уведомлений)."""
    if not trades:
        return []
    new_trades = []
    with _db_lock:
        conn = get_conn()
        for t in trades:
            trade_id = t.get("orderId") or t.get("execId") or f"{t.get('symbol')}_{t.get('updatedTime')}"
            try:
                cur = conn.execute(
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
        conn.commit()
        conn.close()
    return new_trades


def query_trades(start_ms: int, end_ms: int, symbol: str = None) -> list:
    with _db_lock:
        conn = get_conn()
        if symbol:
            rows = conn.execute(
                "SELECT * FROM trades WHERE updated_time BETWEEN ? AND ? AND symbol = ? ORDER BY updated_time ASC",
                (start_ms, end_ms, symbol),
            ).fetchall()
        else:
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


def distinct_symbols() -> list:
    with _db_lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT symbol, COUNT(*) AS c FROM trades GROUP BY symbol ORDER BY c DESC LIMIT 20"
        ).fetchall()
        conn.close()
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

    if NOTIFY_ON_NEW_TRADE and not silent_notify and not is_first_run and new_trades and ALLOWED_CHAT_ID:
        for t in new_trades:
            notify_new_trade(t)

    return len(new_trades)


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
        except Exception as e:
            log.error("Ошибка синхронизации: %s", str(e))
        time.sleep(SYNC_INTERVAL_SEC)


def daily_report_loop():
    """Раз в сутки в заданный час отправляет статистику за прошедшие сутки."""
    if not ENABLE_DAILY_REPORT or not ALLOWED_CHAT_ID:
        return
    last_sent_date = get_meta("last_daily_report_date", "")
    while True:
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

    s = compute_stats(trades)

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

    return "\n".join(lines)


# ---------------------- График капитала (equity curve) ----------------------

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

    lines = [
        "ℹ️ <b>Статус бота</b>\n",
        f"Аптайм: <b>{hours} ч {minutes} мин</b>",
        f"Сделок в базе: <b>{total}</b>",
        f"Последняя синхронизация: <b>{last_sync_str}</b>",
        f"Интервал синхронизации: {SYNC_INTERVAL_SEC} сек",
        f"Категория Bybit: {BYBIT_CATEGORY} | Testnet: {BYBIT_TESTNET}",
        f"Уведомления о сделках: {'вкл' if NOTIFY_ON_NEW_TRADE else 'выкл'}",
        f"Ежедневный отчёт: {'вкл в ' + str(DAILY_REPORT_HOUR_UTC) + ':00 UTC' if ENABLE_DAILY_REPORT else 'выкл'}",
    ]
    return "\n".join(lines)


# ---------------------- Проверка доступа ----------------------

def is_allowed(chat_id) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == str(ALLOWED_CHAT_ID)


# ---------------------- Меню (кнопки) ----------------------

PERIODS = [("Сегодня", 1), ("7 дней", 7), ("30 дней", 30), ("Всё время", 3650)]


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
        types.InlineKeyboardButton("🔄 Синхронизировать", callback_data="sync"),
        types.InlineKeyboardButton("ℹ️ Статус", callback_data="status"),
    )
    return kb


def period_menu(prefix: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(name, callback_data=f"{prefix}_{days}") for name, days in PERIODS]
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
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
            bot.edit_message_text("За какой период показать статистику?", chat_id, msg_id, reply_markup=period_menu("stats"))

        elif data == "menu_chart":
            bot.edit_message_text("За какой период построить график?", chat_id, msg_id, reply_markup=period_menu("chart"))

        elif data == "menu_export":
            bot.edit_message_text("За какой период выгрузить CSV?", chat_id, msg_id, reply_markup=period_menu("export"))

        elif data == "menu_symbols":
            bot.edit_message_text("Выбери инструмент:", chat_id, msg_id, reply_markup=symbols_menu())

        elif data.startswith("stats_"):
            days = int(data.split("_")[1])
            bot.answer_callback_query(call.id, "Считаю...")
            msg = build_stats_message(PERIOD_NAMES.get(days, f"{days} дней"), days)
            bot.edit_message_text(msg, chat_id, msg_id, reply_markup=period_menu("stats"))

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
    threading.Thread(target=daily_report_loop, daemon=True).start()

    log.info("Бот запущен. Ожидаю команды...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.exception("Бот упал, перезапуск через 5 секунд: %s", e)
            time.sleep(5)
