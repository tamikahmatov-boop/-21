from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "PumpAlert Clone работает!"

@app.route("/coins")
def coins():
    return jsonify([
        {"symbol": "BTCUSDT", "change": 2.1},
        {"symbol": "ETHUSDT", "change": 1.5},
        {"symbol": "SOLUSDT", "change": 5.3}
    ])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
