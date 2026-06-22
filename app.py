from flask import Flask, jsonify, send_from_directory
import requests
import threading
import time

app = Flask(__name__)

price_history = {}
alerts = []

INTERVAL = 5
WINDOW = 300
THRESHOLD = 3.0


def monitor():
    global alerts

    while True:
        try:
            response = requests.get(
                "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10
            )

            data = response.json()["data"]

            now = time.time()
            new_alerts = []

            for item in data:

                symbol = item["instId"]

                if not symbol.endswith("-USDT-SWAP"):
                    continue

                price = float(item["last"])

                if symbol not in price_history:
                    price_history[symbol] = []

                price_history[symbol].append((now, price))

                price_history[symbol] = [
                    x for x in price_history[symbol]
                    if now - x[0] <= WINDOW
                ]

                old_price = price_history[symbol][0][1]

                if old_price > 0:

                    change = (price - old_price) / old_price * 100

                    if change >= THRESHOLD:

                        new_alerts.append({
                            "symbol": symbol.replace("-SWAP", ""),
                            "price": round(price, 6),
                            "change_5m": round(change, 2)
                        })

            alerts = sorted(
                new_alerts,
                key=lambda x: x["change_5m"],
                reverse=True
            )

        except Exception as e:
            print(e)

        time.sleep(INTERVAL)


@app.route("/")
def home():
    return send_from_directory("frontend", "index.html")


@app.route("/alerts")
def get_alerts():
    return jsonify(alerts)


threading.Thread(target=monitor, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
