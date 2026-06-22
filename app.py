from flask import Flask, jsonify
import requests

app = Flask(__name__)

@app.route("/")
def home():
    return "PumpAlert Pro работает!"

@app.route("/coins")
def coins():
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=linear"
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        result = []

        if "result" in data and "list" in data["result"]:
            for x in data["result"]["list"]:
                if x["symbol"].endswith("USDT"):
                    result.append({
                        "symbol": x["symbol"],
                        "price": x["lastPrice"],
                        "change": x["price24hPcnt"]
                    })

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
