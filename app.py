from flask import Flask, send_from_directory
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "FRONT WORKS"

@app.route("/test")
def test():
    return "OK"

if __name__ == "__main__":
    app.run()
