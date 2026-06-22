from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "WORKS"

@app.route("/test")
def test():
    return "OK"
