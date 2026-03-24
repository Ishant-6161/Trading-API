from flask import Flask, request, jsonify
import requests
import time
import hmac
import hashlib
import os
import json

app = Flask(__name__)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

BASE_URL = "https://api.delta.exchange"

def generate_signature(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def place_order(side):
    path = "/v2/orders"
    method = "POST"
    timestamp = str(int(time.time()))

    body = {
        "product_id": 27,   # change if needed
        "size": 1,
        "side": side,
        "order_type": "market"
    }

    body_json = json.dumps(body)

    message = method + timestamp + path + body_json
    signature = generate_signature(API_SECRET, message)

    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json"
    }

    response = requests.post(BASE_URL + path, headers=headers, data=body_json)
    return response.json()

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Received:", data)

    signal = data.get("signal")

    if signal == "BUY":
        return jsonify(place_order("buy"))

    elif signal == "SELL":
        return jsonify(place_order("sell"))

    return jsonify({"status": "ignored"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)