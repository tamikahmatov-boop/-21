from flask import Flask, jsonify
import requests

app = Flask(__name__)

@app.route("/")
def home():
    return "PumpAlert Pro работает!"

@app.route("/coins")
def coins():
    try:
        url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        data = response.json()

        result = []

        if "data" in data:
            for x in data["data"]:
                symbol = x["instId"]

                if symbol.endswith("-USDT-SWAP"):
                    result.append({
                        "symbol": symbol.replace("-SWAP", ""),
                        "price": float(x["last"]),
                        "change24h": round(float(x["change24h"]) * 100, 2)
                    })

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
