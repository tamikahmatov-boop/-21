from flask import Flask, send_from_directory, jsonify
import os

app = Flask(__name__, static_folder="frontend")

@app.route("/")
def home():
    return send_from_directory("frontend", "index.html")

@app.route("/test")
def test():
    return "OK"

@app.route("/api/pumps")
def pumps():
    return jsonify([])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
