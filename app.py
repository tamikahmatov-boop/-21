from flask import Flask, jsonify
import requests

app = Flask(__name__)

@app.route("/")
def home():
    return "PumpAlert Pro работает!"

@app.route("/coins")
def coins():
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    data = requests.get(url, timeout=10).json()["result"]["list"]

    result = []
    for x in data:
        if x["symbol"].endswith("USDT"):
            result.append({
                "symbol": x["symbol"],
                "price": x["lastPrice"],
                "change": x["price24hPcnt"]
            })

    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
