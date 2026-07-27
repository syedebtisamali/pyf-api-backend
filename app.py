"""
Pakistan Youth Foundation — Dashboard Backend
-----------------------------------------------
Flask serves every authenticated page as a fully-rendered HTML document.
The frontend (dashboard.html / profile.html / etc.) fetches these routes
with credentials included and swaps the whole document, so every route
here returns a complete <html> page (never a JSON fragment) EXCEPT the
small AJAX helper endpoints used by the Progress page console
(/progress/data, /progress/add, /progress/update) and /add_application,
which return JSON for the JS on the page to consume.

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
Report page always reflects the funding history.
"""
import os
import flask
from flask import session, request, jsonify, render_template
from flask_cors import CORS
from pymongo import MongoClient
from functools import wraps
from datetime import datetime

app = flask.Flask(__name__, static_folder="static", template_folder="templates")

# Pull the secret key from Vercel environment variables, fallback for local testing
app.secret_key = os.environ.get("SECRET_KEY", "super_secret_local_key")

CORS(app, supports_credentials=True, origins=["*"], allow_headers=["Content-Type"])

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
            return "<h3>Unauthorized access. Please login first.</h3>", 401
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
    the progress.html template (and the JSON console endpoints) expect."""
    total = float(progress.get("TOTAL_FUND", 0) or 0)
    obtained = float(progress.get("OBTAINED_FUND", 0) or 0)
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


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return "Welcome to the Flask App!"


@app.route("/dashboard", methods=["POST"])
def dashboard():
    data = request.get_json(silent=True)

    if not data:
        return "<h1>Error</h1><p>No data received.</p>", 400

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    user = users_col.find_one({"USERNAME": username})

    if user and user.get("PASSWORD") == password and user.get("IS_ACTIVE", True):
        session["username"] = username
        return render_template(
            "profile.html", user_name=username, role=user.get("ROLE", "Member")
        )

    return "<h1>Login Failed</h1><p>Invalid credentials. Please try again.</p>", 401


# ---------------------------------------------------------------------------
# Authenticated page routes
# ---------------------------------------------------------------------------
@app.route("/profile", methods=["GET"])
@login_required
def profile():
    user = get_current_user()
    return render_template(
        "profile.html",
        user_name=session["username"],
        role=user.get("ROLE", "Member") if user else "Member",
    )


@app.route("/experience", methods=["GET"])
@login_required
def experience():
    user = get_current_user()
    exp = (user or {}).get("EXPERIENCE", {})
    return render_template(
        "experience.html",
        user_name=session["username"],
        user_role=exp.get("USER_ROLE", "Member"),
        start_date=exp.get("START_DATE", "N/A"),
        end_date=exp.get("END_DATE", "Present"),
    )


@app.route("/applications", methods=["GET"])
@login_required
def applications():
    user = get_current_user()
    apps = (user or {}).get("APPLICATION", {})
    return render_template(
        "applications.html", user_name=session["username"], applications=apps
    )


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
    return render_template(
        "contributions.html",
        user_name=session["username"],
        contribution_data=contribution_data,
    )


@app.route("/progress", methods=["GET"])
@login_required
def progress():
    user = get_current_user()
    data = compute_progress((user or {}).get("PROGRESS", {}))
    return render_template("progress.html", user_name=session["username"], **data)


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
    total_submitted = sum(float(r.get("SUBMITTED_AMOUNT", 0) or 0) for r in reports_sorted)
    return render_template(
        "report.html",
        user_name=session["username"],
        report=reports_sorted,
        total_submitted=total_submitted,
    )


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.pop("username", None)
    return jsonify({"success": True, "message": "Logged out successfully"}), 200



if __name__ == "__main__":
    app.run(debug=True, port=5050)
