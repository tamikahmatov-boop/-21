from flask import Flask, send_from_directory, jsonify
import os
import time
import threading
import requests

app = Flask(__name__, static_folder="frontend")

# ---------------- FRONTEND ----------------

@app.route("/")
def home():
    return send_from_directory("frontend", "index.html")


# ---------------- HEALTH CHECK ----------------

@app.route("/test")
def test():
    return "OK"


# ---------------- OKX DATA ----------------

def get_okx_symbols():
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()["data"]
        return data
    except:
        return []


price_history = {}
pumps = []


def monitor():
    global price_history, pumps

    while True:
        data = get_okx_symbols()
        new_pumps = []

        for item in data:
            try:
                symbol = item["instId"]
                price = float(item["last"])

                if symbol not in price_history:
                    price_history[symbol] = []

                price_history[symbol].append(price)

                # держим последние 5 значений
                if len(price_history[symbol]) > 5:
                    price_history[symbol].pop(0)

                old_price = price_history[symbol][0]

                change = ((price - old_price) / old_price) * 100

                if change >= 3:
                    new_pumps.append({
                        "symbol": symbol,
                        "change": round(change, 2),
                        "price": price
                    })

            except:
                continue

        pumps = new_pumps
        time.sleep(5)


@app.route("/api/pumps")
def get_pumps():
    return jsonify(pumps)


# ---------------- START BACKGROUND THREAD ----------------

threading.Thread(target=monitor, daemon=True).start()


# ---------------- RUN ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
