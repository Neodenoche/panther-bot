from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import json
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB_FILE = "/data/panther_db.json"


# ───────────────
# DB HELPERS
# ───────────────
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_db(db):
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    with open(DB_FILE, "w") as f:
        json.dump(db, f)


# ───────────────
# 🐆 SERVIR MINI APP
# ───────────────
@app.route("/")
def serve_app():
    return send_file("index.html")


# ───────────────
# DEBUG
# ───────────────
@app.route("/debug")
def debug():
    db_exists = os.path.exists(DB_FILE)
    db_size = os.path.getsize(DB_FILE) if db_exists else 0
    db = load_db()

    return jsonify({
        "db_file": DB_FILE,
        "db_exists": db_exists,
        "db_size": db_size,
        "user_count": len(db),
        "users": list(db.keys())
    })


# ───────────────
# USER
# ───────────────
@app.route("/user")
def get_user():
    user_id = request.args.get("id")
    if not user_id:
        return jsonify({"error": "Missing id"}), 400

    db = load_db()

    if user_id not in db:
        db[user_id] = {
            "id": user_id,
            "points": 0,
            "streak": 0,
            "level": "🐾 Cachorro",
            "history": [],
            "last_checkin": None
        }
        save_db(db)

    return jsonify(db[user_id])


# ───────────────
# CHECK-IN
# ───────────────
@app.route("/checkin")
def checkin():
    user_id = request.args.get("id")
    if not user_id:
        return jsonify({"error": "Missing id"}), 400

    db = load_db()

    if user_id not in db:
        return jsonify({"error": "User not found"}), 404

    user = db[user_id]
    today = datetime.utcnow().date().isoformat()

    if user.get("last_checkin") == today:
        return jsonify({"status": "already_checked"})

    user["points"] += 10
    user["streak"] += 1
    user["last_checkin"] = today

    db[user_id] = user
    save_db(db)

    return jsonify({
        "status": "ok",
        "points": user["points"]
    })


# ───────────────
# HEALTH CHECK
# ───────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ───────────────
# RUN (FIX RAILWAY)
# ───────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
