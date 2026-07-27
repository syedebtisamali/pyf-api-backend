"""
Pakistan Youth Foundation — Backend API (Flask, JSON-only)
-----------------------------------------------------------
Pure JSON API — every route returns JSON, none render HTML. Meant to be
called cross-origin (e.g. from a static frontend on Netlify) via fetch()
with credentials included, which is why SESSION_COOKIE_SAMESITE is "None"
and CORS is configured with supports_credentials + an explicit origin
list below — set ALLOWED_ORIGINS to your deployed frontend URL(s).

Data model (one document per member in the `users` collection):

{
  "USERNAME": "SEA",
  "PASSWORD": "123",              # NOTE: plaintext to match the brief;
                                   # swap for a hashed password in production.
  "ROLE": "Admin",
  "IS_ACTIVE": true,
  "APPLICATION": { "APP1": {...}, "APP2": {...} },
  "CONTRIBUTIONS": { "TITLE1": "DESCRIPTION1", ... },
  "PROGRESS": { "TOTAL_FUND": 10000, "OBTAINED_FUND": 8000 },
  "REPORT": [ { "DATE": "...", "SUBMITTED_AMOUNT": 5000 }, ... ],
  "EXPERIENCE": { "USER_ROLE": "CEO", "START_DATE": "...", "END_DATE": "Present" }
}

Every time an amount is added on the Progress page, it both updates
PROGRESS.OBTAINED_FUND *and* appends a timestamped entry to REPORT, so the
Report endpoint always reflects the funding history.
"""
import os
import flask
from flask import session, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from functools import wraps
from datetime import datetime

app = flask.Flask(__name__)

# --- SESSION COOKIE CONFIGURATION ---
# Required for a cross-origin frontend (e.g. Netlify) to keep a session
# cookie across requests to this API. Without SameSite=None + Secure,
# browsers silently drop the cookie on cross-site requests — login looks
# like it works, but every subsequent request appears logged out.
app.config.update(
    SESSION_COOKIE_SECURE=True,      # Required for HTTPS + SameSite=None
    SESSION_COOKIE_HTTPONLY=True,    # Prevents JavaScript client-side theft
    SESSION_COOKIE_SAMESITE="None",  # Required for the cross-origin frontend
)

# Pull the secret key from Vercel environment variables, fallback for local testing
app.secret_key = os.environ.get("SECRET_KEY", "super_secret_local_key")

# --- CORS ---
# IMPORTANT: browsers reject `origins=["*"]` combined with credentials — a
# wildcard Access-Control-Allow-Origin is never honored when the request
# carries cookies. Set ALLOWED_ORIGINS on your host to your deployed
# frontend URL(s), comma-separated, e.g.:
#   ALLOWED_ORIGINS=https://pyf-admin-panel.netlify.app
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:5500,http://localhost:8888"
    ).split(",") if o.strip()
]
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS, allow_headers=["Content-Type"])

# Pull the Mongo URI from Vercel environment variables, fallback for local testing
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")

client = MongoClient(MONGO_URI)
db = client["PYF_DATABASE"]
users_col = db["USERS"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def login_required(view_func):
    """Guards a route behind an active session, matching the original
    per-route check but avoiding repeating it six times."""

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return jsonify({"error": "Unauthorized access. Please login first."}), 401
        return view_func(*args, **kwargs)

    return wrapped


def get_current_user():
    return users_col.find_one({"USERNAME": session.get("username")})


def compute_zone(percentage):
    if percentage >= 75:
        return "Safe"
    if percentage >= 40:
        return "Warning"
    return "Critical"


def compute_progress(progress):
    """Turns the raw PROGRESS sub-document into the display-ready numbers
    the frontend expects. Tolerant of TOTAL_FUND/OBTAINED_FUND being
    stored as strings or missing/malformed values."""
    try:
        total = float(progress.get("TOTAL_FUND", 0) or 0)
    except (TypeError, ValueError):
        total = 0.0
    try:
        obtained = float(progress.get("OBTAINED_FUND", 0) or 0)
    except (TypeError, ValueError):
        obtained = 0.0
    percentage = round((obtained / total * 100), 1) if total > 0 else 0
    percentage = min(percentage, 100)
    remaining = max(total - obtained, 0)
    return {
        "target_amount": total,
        "current_amount": obtained,
        "remaining_amount": remaining,
        "percentage": percentage,
        "zone": compute_zone(percentage),
    }


def normalize_reports(reports):
    """REPORT used to be a single object in early drafts of the schema;
    normalize it to a list so old documents don't break the page."""
    if isinstance(reports, dict):
        return [reports] if reports else []
    return reports or []


def report_amount(entry):
    try:
        return float(entry.get("SUBMITTED_AMOUNT", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return jsonify({"success": True, "message": "PYF backend API is running."}), 200


@app.route("/dashboard", methods=["POST"])
def dashboard():
    """Member login. Kept the historical /dashboard path so existing
    frontend code doesn't need a route rename, but it returns JSON like
    every other endpoint instead of a rendered profile page."""
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "No data received."}), 400

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    user = users_col.find_one({"USERNAME": username})

    if user and user.get("PASSWORD") == password and user.get("IS_ACTIVE", True):
        session["username"] = username
        return jsonify({
            "success": True,
            "user_name": username,
            "role": user.get("ROLE", "Member"),
        }), 200

    return jsonify({"error": "Invalid credentials. Please try again."}), 401


# ---------------------------------------------------------------------------
# Authenticated routes
# ---------------------------------------------------------------------------
@app.route("/profile", methods=["GET"])
@login_required
def profile():
    user = get_current_user()
    return jsonify({
        "success": True,
        "user_name": session["username"],
        "role": user.get("ROLE", "Member") if user else "Member",
    }), 200


@app.route("/experience", methods=["GET"])
@login_required
def experience():
    user = get_current_user()
    exp = (user or {}).get("EXPERIENCE", {})
    return jsonify({
        "success": True,
        "user_name": session["username"],
        "user_role": exp.get("USER_ROLE", "Member"),
        "start_date": exp.get("START_DATE", "N/A"),
        "end_date": exp.get("END_DATE", "Present"),
    }), 200


@app.route("/applications", methods=["GET"])
@login_required
def applications():
    user = get_current_user()
    apps = (user or {}).get("APPLICATION", {})
    out = [{"app_key": k, **v} for k, v in apps.items()]
    out.sort(key=lambda a: a.get("SUBMITTED_AT") or "", reverse=True)
    return jsonify({
        "success": True,
        "user_name": session["username"],
        "applications": out,
    }), 200


@app.route("/add_application", methods=["POST"])
@login_required
def add_application():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()

    if not title or not content:
        return jsonify({"error": "Title and content are required."}), 400

    user = get_current_user()
    apps = (user or {}).get("APPLICATION", {})
    next_num = len(apps) + 1
    app_key = f"APP{next_num}"

    new_app = {
        "ID": f"APP-{100 + next_num}",
        "TITLE": title,
        "CONTENT": content,
        "STATUS": "Pending",
        "SUBMITTED_AT": datetime.now().strftime("%d-%m-%Y %H:%M"),
    }

    users_col.update_one(
        {"USERNAME": session["username"]}, {"$set": {f"APPLICATION.{app_key}": new_app}}
    )

    return jsonify({"success": True, "application": new_app}), 200


@app.route("/contributions", methods=["GET"])
@login_required
def contributions():
    user = get_current_user()
    raw = (user or {}).get("CONTRIBUTIONS", {})
    contribution_data = [{"title": k, "description": v} for k, v in raw.items()]
    return jsonify({
        "success": True,
        "user_name": session["username"],
        "contributions": contribution_data,
    }), 200


@app.route("/progress", methods=["GET"])
@login_required
def progress():
    user = get_current_user()
    data = compute_progress((user or {}).get("PROGRESS", {}))
    return jsonify({"success": True, "user_name": session["username"], "progress": data}), 200


@app.route("/progress/data", methods=["GET"])
@login_required
def progress_data():
    user = get_current_user()
    data = compute_progress((user or {}).get("PROGRESS", {}))
    return jsonify({"success": True, "progress": data}), 200


@app.route("/progress/add", methods=["POST"])
@login_required
def progress_add():
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid numeric amount."}), 400

    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero."}), 400

    user = get_current_user()
    progress_doc = (user or {}).get("PROGRESS", {"TOTAL_FUND": 0, "OBTAINED_FUND": 0})
    new_obtained = float(progress_doc.get("OBTAINED_FUND", 0) or 0) + amount

    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M")
    entry = {"DATE": timestamp, "SUBMITTED_AMOUNT": amount}

    reports = normalize_reports((user or {}).get("REPORT", []))
    reports.append(entry)

    users_col.update_one(
        {"USERNAME": session["username"]},
        {"$set": {"PROGRESS.OBTAINED_FUND": new_obtained, "REPORT": reports}},
    )

    updated = compute_progress({**progress_doc, "OBTAINED_FUND": new_obtained})
    return jsonify({"success": True, "progress": updated, "entry": entry}), 200


@app.route("/progress/subtract", methods=["POST"])
@login_required
def progress_subtract():
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid numeric amount."}), 400

    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero."}), 400

    user = get_current_user()
    progress_doc = (user or {}).get("PROGRESS", {"TOTAL_FUND": 0, "OBTAINED_FUND": 0})
    current = float(progress_doc.get("OBTAINED_FUND", 0) or 0)
    new_obtained = max(current - amount, 0)

    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M")
    entry = {"DATE": timestamp, "SUBMITTED_AMOUNT": -amount}

    reports = normalize_reports((user or {}).get("REPORT", []))
    reports.append(entry)

    users_col.update_one(
        {"USERNAME": session["username"]},
        {"$set": {"PROGRESS.OBTAINED_FUND": new_obtained, "REPORT": reports}},
    )

    updated = compute_progress({**progress_doc, "OBTAINED_FUND": new_obtained})
    return jsonify({"success": True, "progress": updated, "entry": entry}), 200


@app.route("/progress/update", methods=["POST"])
@login_required
def progress_update():
    data = request.get_json(silent=True) or {}
    try:
        target = float(data.get("target_amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid numeric target."}), 400

    if target < 0:
        return jsonify({"error": "Target must be zero or greater."}), 400

    users_col.update_one(
        {"USERNAME": session["username"]}, {"$set": {"PROGRESS.TOTAL_FUND": target}}
    )

    user = get_current_user()
    updated = compute_progress((user or {}).get("PROGRESS", {}))
    return jsonify({"success": True, "progress": updated}), 200


@app.route("/report", methods=["GET"])
@login_required
def report():
    user = get_current_user()
    reports = normalize_reports((user or {}).get("REPORT", []))
    reports_sorted = sorted(reports, key=lambda r: r.get("DATE", ""), reverse=True)
    total_submitted = sum(report_amount(r) for r in reports_sorted)
    return jsonify({
        "success": True,
        "user_name": session["username"],
        "report": reports_sorted,
        "total_submitted": total_submitted,
    }), 200


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.pop("username", None)
    return jsonify({"success": True, "message": "Logged out successfully"}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5050)
