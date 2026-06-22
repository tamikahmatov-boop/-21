from flask import Flask, send_from_directory
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
    return []

if __name__ == "__main__":
    app.run()
