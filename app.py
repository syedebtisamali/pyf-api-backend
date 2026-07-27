"""
Pakistan Youth Foundation — Dashboard Backend
-----------------------------------------------
Flask serves every authenticated MEMBER-facing page as a fully-rendered HTML
document (unchanged from the original brief). The ADMIN side is now a JSON
API consumed by a single self-contained control panel page served at
GET /admin/panel (templates/admin_panel.html). Because that page is served
from this same Flask app, all of its fetch() calls are same-origin — no
CORS/third-party-cookie headaches, even though SESSION_COOKIE_SAMESITE is
set to "None" for the member-facing cross-origin frontend.

Data model (one document per member in the `users` collection) — unchanged,
plus one new field this revision adds:

{
  "USERNAME": "SEA",
  "PASSWORD": "123",              # NOTE: plaintext to match the brief;
                                   # swap for a hashed password in production.
  "ROLE": "Admin",
  "IS_ACTIVE": true,
  "LAST_ACTIVE": "2026-07-27T00:35:00.000000",  # NEW — ISO timestamp, bumped
                                                  # on every authenticated
                                                  # request. Powers "active
                                                  # N min ago" in the panel.
  "APPLICATION": { "APP1": {...}, ... },
  "CONTRIBUTIONS": { "TITLE1": "DESCRIPTION1", ... },
  "PROGRESS": { "TOTAL_FUND": 10000, "OBTAINED_FUND": 8000 },
  "REPORT": [ { "DATE": "...", "SUBMITTED_AMOUNT": 5000 }, ... ],
  "EXPERIENCE": { "USER_ROLE": "CEO", "START_DATE": "...", "END_DATE": "Present" }
}

REPORT dates are stored as either "D-M-YYYY" or "D-M-YYYY HH:MM" (both seen
in production data, sometimes without zero-padding). parse_report_date()
below tries every format actually seen before giving up.

Every time an amount is added on the Progress page, it both updates
PROGRESS.OBTAINED_FUND *and* appends a timestamped entry to REPORT, so the
Report page and the admin weekly reports always reflect the funding history.
"""
import os
from datetime import datetime, timedelta

import flask
from flask import session, request, jsonify, render_template
from flask_cors import CORS
from pymongo import MongoClient
from bson import ObjectId
from functools import wraps

app = flask.Flask(__name__, static_folder="static", template_folder="templates")

# --- SESSION COOKIE CONFIGURATION ---
app.config.update(
    SESSION_COOKIE_SECURE=True,      # Required for HTTPS
    SESSION_COOKIE_HTTPONLY=True,    # Prevents JavaScript client-side theft
    SESSION_COOKIE_SAMESITE='None'   # Allows session cookies to stick across redirects
)

# Pull the secret key from Vercel environment variables, fallback for local testing
app.secret_key = os.environ.get("SECRET_KEY", "super_secret_local_key")

# --- CORS ---
# IMPORTANT: browsers reject `origins=["*"]` combined with credentials — a
# wildcard Access-Control-Allow-Origin is never honored when the request
# carries cookies. List the real origin(s) of your member-facing frontend
# here (e.g. your Vercel site). The admin panel itself is served BY this
# app at /admin/panel, so it's same-origin and never touches this list.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:5500"
    ).split(",") if o.strip()
]
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS, allow_headers=["Content-Type"])

# Pull the Mongo URI from Vercel environment variables, fallback for local testing
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")

client = MongoClient(MONGO_URI)
db = client["PYF_DATABASE"]
users_col = db["USERS"]

ONLINE_WINDOW_MINUTES = 5
REPORT_DATE_FORMATS = ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y", "%d-%m-%Y %H:%M:%S.%f")


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


def api_login_required(view_func):
    """Same guard as login_required but replies with JSON — for the admin
    control-panel API rather than the server-rendered member pages."""

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return jsonify({"error": "Not authenticated. Please log in."}), 401
        return view_func(*args, **kwargs)

    return wrapped


def api_admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return jsonify({"error": "Not authenticated. Please log in."}), 401
        user = get_current_user()
        if not user or user.get("ROLE") != "Admin":
            return jsonify({"error": "Admins only."}), 403
        return view_func(*args, **kwargs)

    return wrapped


@app.before_request
def track_activity():
    """Bumps LAST_ACTIVE for whoever is logged in, on every request they
    make while authenticated. This is what lets the admin panel show
    'active 3 min ago' / 'online now' per member."""
    if "username" in session:
        users_col.update_one(
            {"USERNAME": session["username"]},
            {"$set": {"LAST_ACTIVE": datetime.utcnow().isoformat()}},
        )


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
    the progress.html template (and the JSON console endpoints) expect.
    Tolerant of TOTAL_FUND/OBTAINED_FUND being stored as strings."""
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


def parse_report_date(date_str):
    """Best-effort parse of the DATE strings actually found in REPORT
    entries. Returns None (rather than raising) on anything unrecognized,
    so one malformed entry can't take down a whole report."""
    if not date_str:
        return None
    for fmt in REPORT_DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def report_amount(entry):
    try:
        return float(entry.get("SUBMITTED_AMOUNT", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def is_online(last_active_iso):
    if not last_active_iso:
        return False
    try:
        ts = datetime.fromisoformat(last_active_iso)
    except ValueError:
        return False
    return datetime.utcnow() - ts <= timedelta(minutes=ONLINE_WINDOW_MINUTES)


def minutes_since(last_active_iso):
    if not last_active_iso:
        return None
    try:
        ts = datetime.fromisoformat(last_active_iso)
    except ValueError:
        return None
    return round((datetime.utcnow() - ts).total_seconds() / 60, 1)


def serialize_user_public(u):
    """Member summary shape used across the admin panel's list views."""
    progress = compute_progress(u.get("PROGRESS", {}) or {})
    apps = (u.get("APPLICATION", {}) or {})
    pending = sum(1 for a in apps.values() if a.get("STATUS") == "Pending")
    return {
        "username": u.get("USERNAME"),
        "role": u.get("ROLE", "Member"),
        "is_active": u.get("IS_ACTIVE", True),
        "last_active": u.get("LAST_ACTIVE"),
        "minutes_since_active": minutes_since(u.get("LAST_ACTIVE")),
        "online": is_online(u.get("LAST_ACTIVE")),
        "experience": u.get("EXPERIENCE", {}),
        "progress": progress,
        "applications_count": len(apps),
        "pending_applications": pending,
        "contributions_count": len(u.get("CONTRIBUTIONS", {}) or {}),
    }


def serialize_user_raw(u):
    """Full document, JSON-safe (ObjectId -> str), for the raw JSON editor."""
    out = dict(u)
    if "_id" in out:
        out["_id"] = str(out["_id"])
    return out


# ---------------------------------------------------------------------------
# Public / member routes (unchanged behavior)
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
    total_submitted = sum(report_amount(r) for r in reports_sorted)
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


# ---------------------------------------------------------------------------
# Admin control panel — page + JSON API
# ---------------------------------------------------------------------------
@app.route("/admin/panel", methods=["GET"])
def admin_panel_page():
    """Serves the single-page admin control panel. The page itself checks
    /admin/me on load and shows its own login screen if there's no active
    admin session — this route is intentionally not guarded so the login
    screen can load."""
    return render_template("admin_panel.html")


@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    user = users_col.find_one({"USERNAME": username})
    if not user or user.get("PASSWORD") != password:
        return jsonify({"error": "Invalid credentials."}), 401
    if not user.get("IS_ACTIVE", True):
        return jsonify({"error": "This account has been deactivated."}), 403
    if user.get("ROLE") != "Admin":
        return jsonify({"error": "This account does not have admin access."}), 403

    session["username"] = username
    return jsonify({"success": True, "username": username, "role": user.get("ROLE")}), 200


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("username", None)
    return jsonify({"success": True}), 200


@app.route("/admin/me", methods=["GET"])
def admin_me():
    if "username" not in session:
        return jsonify({"authenticated": False}), 200
    user = get_current_user()
    if not user or user.get("ROLE") != "Admin":
        return jsonify({"authenticated": False}), 200
    return jsonify({
        "authenticated": True,
        "username": user.get("USERNAME"),
        "role": user.get("ROLE"),
    }), 200


# --- Overview -----------------------------------------------------------
@app.route("/admin/overview", methods=["GET"])
@api_admin_required
def admin_overview():
    all_users = list(users_col.find({}))

    total_target = total_obtained = 0.0
    pending_apps = total_apps = 0
    online_now = 0
    transactions = []

    for u in all_users:
        p = compute_progress(u.get("PROGRESS", {}) or {})
        total_target += p["target_amount"]
        total_obtained += p["current_amount"]

        apps = u.get("APPLICATION", {}) or {}
        total_apps += len(apps)
        pending_apps += sum(1 for a in apps.values() if a.get("STATUS") == "Pending")

        if is_online(u.get("LAST_ACTIVE")):
            online_now += 1

        for entry in normalize_reports(u.get("REPORT", [])):
            transactions.append({
                "username": u.get("USERNAME"),
                "date": entry.get("DATE"),
                "amount": report_amount(entry),
            })

    parsed = [(t, parse_report_date(t["date"])) for t in transactions]
    parsed.sort(key=lambda pair: pair[1] or datetime.min, reverse=True)
    recent_transactions = [t for t, _ in parsed[:15]]

    overall_progress = compute_progress({"TOTAL_FUND": total_target, "OBTAINED_FUND": total_obtained})

    return jsonify({
        "success": True,
        "total_members": len(all_users),
        "active_members": sum(1 for u in all_users if u.get("IS_ACTIVE", True)),
        "online_now": online_now,
        "total_applications": total_apps,
        "pending_applications": pending_apps,
        "overall_progress": overall_progress,
        "recent_transactions": recent_transactions,
    }), 200


# --- Members / Profiles --------------------------------------------------
@app.route("/admin/members", methods=["GET"])
@api_admin_required
def admin_list_members():
    all_users = list(users_col.find({}))
    members = [serialize_user_public(u) for u in all_users]
    members.sort(key=lambda m: m["username"] or "")
    return jsonify({"success": True, "members": members}), 200


@app.route("/admin/members", methods=["POST"])
@api_admin_required
def admin_create_member():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "Member").strip()
    user_role = (data.get("user_role") or role).strip()

    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400
    if users_col.find_one({"USERNAME": username}):
        return jsonify({"error": "That username already exists."}), 409

    new_user = {
        "USERNAME": username,
        "PASSWORD": password,
        "ROLE": role,
        "IS_ACTIVE": True,
        "LAST_ACTIVE": None,
        "APPLICATION": {},
        "CONTRIBUTIONS": {},
        "PROGRESS": {"TOTAL_FUND": 0, "OBTAINED_FUND": 0},
        "REPORT": [],
        "EXPERIENCE": {
            "USER_ROLE": user_role,
            "START_DATE": datetime.now().strftime("%d-%m-%Y"),
            "END_DATE": "Present",
        },
    }
    users_col.insert_one(new_user)
    return jsonify({"success": True, "member": serialize_user_public(new_user)}), 201


@app.route("/admin/members/<username>", methods=["PATCH"])
@api_admin_required
def admin_update_member(username):
    target = users_col.find_one({"USERNAME": username})
    if not target:
        return jsonify({"error": "User not found."}), 404

    data = request.get_json(silent=True) or {}
    updates = {}

    if "role" in data and data["role"]:
        updates["ROLE"] = data["role"].strip()
    if "password" in data and data["password"]:
        updates["PASSWORD"] = data["password"]
    if "is_active" in data:
        updates["IS_ACTIVE"] = bool(data["is_active"])
    if "experience" in data and isinstance(data["experience"], dict):
        exp = data["experience"]
        if "USER_ROLE" in exp:
            updates["EXPERIENCE.USER_ROLE"] = exp["USER_ROLE"]
        if "START_DATE" in exp:
            updates["EXPERIENCE.START_DATE"] = exp["START_DATE"]
        if "END_DATE" in exp:
            updates["EXPERIENCE.END_DATE"] = exp["END_DATE"]

    if not updates:
        return jsonify({"error": "No valid fields to update."}), 400

    users_col.update_one({"USERNAME": username}, {"$set": updates})
    updated = users_col.find_one({"USERNAME": username})
    return jsonify({"success": True, "member": serialize_user_public(updated)}), 200


@app.route("/admin/members/<username>/toggle_active", methods=["POST"])
@api_admin_required
def admin_toggle_member_active(username):
    target = users_col.find_one({"USERNAME": username})
    if not target:
        return jsonify({"error": "User not found."}), 404

    new_status = not target.get("IS_ACTIVE", True)
    users_col.update_one({"USERNAME": username}, {"$set": {"IS_ACTIVE": new_status}})
    return jsonify({"success": True, "username": username, "is_active": new_status}), 200


@app.route("/admin/members/<username>", methods=["DELETE"])
@api_admin_required
def admin_delete_member(username):
    if username == session.get("username"):
        return jsonify({"error": "You can't delete the account you're logged in as."}), 400

    target = users_col.find_one({"USERNAME": username})
    if not target:
        return jsonify({"error": "User not found."}), 404

    if target.get("ROLE") == "Admin":
        remaining_admins = users_col.count_documents({"ROLE": "Admin"})
        if remaining_admins <= 1:
            return jsonify({"error": "Can't delete the last remaining admin."}), 400

    users_col.delete_one({"USERNAME": username})
    return jsonify({"success": True, "username": username}), 200


# --- Raw JSON editor -------------------------------------------------------
@app.route("/admin/members/<username>/raw", methods=["GET"])
@api_admin_required
def admin_get_member_raw(username):
    user = users_col.find_one({"USERNAME": username})
    if not user:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"success": True, "document": serialize_user_raw(user)}), 200


@app.route("/admin/members/<username>/raw", methods=["PUT"])
@api_admin_required
def admin_put_member_raw(username):
    existing = users_col.find_one({"USERNAME": username})
    if not existing:
        return jsonify({"error": "User not found."}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object."}), 400

    # _id is immutable and USERNAME stays the route param's value, so a
    # rename doesn't silently orphan the document or collide with another.
    data.pop("_id", None)
    data["USERNAME"] = username

    if not isinstance(data.get("PASSWORD", ""), str) or data.get("PASSWORD") is None:
        data["PASSWORD"] = existing.get("PASSWORD", "")

    users_col.replace_one({"USERNAME": username}, data)
    updated = users_col.find_one({"USERNAME": username})
    return jsonify({"success": True, "document": serialize_user_raw(updated)}), 200


# --- Applications ----------------------------------------------------------
@app.route("/admin/applications", methods=["GET"])
@api_admin_required
def admin_list_applications():
    all_users = list(users_col.find({}))
    out = []
    for u in all_users:
        for app_key, app_data in (u.get("APPLICATION", {}) or {}).items():
            out.append({
                "username": u.get("USERNAME"),
                "app_key": app_key,
                "id": app_data.get("ID"),
                "title": app_data.get("TITLE"),
                "content": app_data.get("CONTENT"),
                "status": app_data.get("STATUS", "Pending"),
                "submitted_at": app_data.get("SUBMITTED_AT"),
            })
    out.sort(key=lambda a: a.get("submitted_at") or "", reverse=True)
    return jsonify({"success": True, "applications": out}), 200


@app.route("/admin/applications/<username>", methods=["POST"])
@api_admin_required
def admin_add_application(username):
    target = users_col.find_one({"USERNAME": username})
    if not target:
        return jsonify({"error": "User not found."}), 404

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title or not content:
        return jsonify({"error": "Title and content are required."}), 400

    apps = target.get("APPLICATION", {}) or {}
    next_num = len(apps) + 1
    app_key = f"APP{next_num}"
    new_app = {
        "ID": f"APP-{100 + next_num}",
        "TITLE": title,
        "CONTENT": content,
        "STATUS": data.get("status", "Pending"),
        "SUBMITTED_AT": datetime.now().strftime("%d-%m-%Y %H:%M"),
    }
    users_col.update_one({"USERNAME": username}, {"$set": {f"APPLICATION.{app_key}": new_app}})
    return jsonify({"success": True, "application": {**new_app, "username": username, "app_key": app_key}}), 201


@app.route("/admin/applications/<username>/<app_key>", methods=["PATCH"])
@api_admin_required
def admin_update_application(username, app_key):
    data = request.get_json(silent=True) or {}
    updates = {}
    if "status" in data and data["status"]:
        updates[f"APPLICATION.{app_key}.STATUS"] = data["status"]
    if "title" in data and data["title"]:
        updates[f"APPLICATION.{app_key}.TITLE"] = data["title"]
    if "content" in data and data["content"]:
        updates[f"APPLICATION.{app_key}.CONTENT"] = data["content"]

    if not updates:
        return jsonify({"error": "Nothing to update."}), 400

    result = users_col.update_one({"USERNAME": username}, {"$set": updates})
    if result.matched_count == 0:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"success": True}), 200


@app.route("/admin/applications/<username>/<app_key>", methods=["DELETE"])
@api_admin_required
def admin_delete_application(username, app_key):
    result = users_col.update_one(
        {"USERNAME": username}, {"$unset": {f"APPLICATION.{app_key}": ""}}
    )
    if result.matched_count == 0:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"success": True}), 200


# --- Contributions -----------------------------------------------------
@app.route("/admin/contributions", methods=["GET"])
@api_admin_required
def admin_list_contributions():
    all_users = list(users_col.find({}))
    out = []
    for u in all_users:
        items = [{"title": k, "description": v} for k, v in (u.get("CONTRIBUTIONS", {}) or {}).items()]
        out.append({"username": u.get("USERNAME"), "items": items})
    return jsonify({"success": True, "contributions": out}), 200


@app.route("/admin/contributions/<username>", methods=["POST"])
@api_admin_required
def admin_add_contribution(username):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    if not title or not description:
        return jsonify({"error": "Title and description are required."}), 400

    result = users_col.update_one(
        {"USERNAME": username}, {"$set": {f"CONTRIBUTIONS.{title}": description}}
    )
    if result.matched_count == 0:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"success": True}), 200


@app.route("/admin/contributions/<username>/<title>", methods=["DELETE"])
@api_admin_required
def admin_delete_contribution(username, title):
    result = users_col.update_one(
        {"USERNAME": username}, {"$unset": {f"CONTRIBUTIONS.{title}": ""}}
    )
    if result.matched_count == 0:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"success": True}), 200


# --- Progress / funding --------------------------------------------------
@app.route("/admin/progress", methods=["GET"])
@api_admin_required
def admin_progress_overview():
    all_users = list(users_col.find({}))
    members = []
    total_target = total_obtained = 0.0
    for u in all_users:
        p = compute_progress(u.get("PROGRESS", {}) or {})
        total_target += p["target_amount"]
        total_obtained += p["current_amount"]
        members.append({"username": u.get("USERNAME"), **p})

    overall = compute_progress({"TOTAL_FUND": total_target, "OBTAINED_FUND": total_obtained})
    return jsonify({"success": True, "overall": overall, "members": members}), 200


@app.route("/admin/progress/<username>/add", methods=["POST"])
@api_admin_required
def admin_progress_add(username):
    return _admin_progress_delta(username, sign=1)


@app.route("/admin/progress/<username>/subtract", methods=["POST"])
@api_admin_required
def admin_progress_subtract(username):
    return _admin_progress_delta(username, sign=-1)


def _admin_progress_delta(username, sign):
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid numeric amount."}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero."}), 400

    user = users_col.find_one({"USERNAME": username})
    if not user:
        return jsonify({"error": "User not found."}), 404

    progress_doc = user.get("PROGRESS", {"TOTAL_FUND": 0, "OBTAINED_FUND": 0}) or {}
    current = float(progress_doc.get("OBTAINED_FUND", 0) or 0)
    new_obtained = current + (amount * sign) if sign > 0 else max(current - amount, 0)

    timestamp = datetime.now().strftime("%d-%m-%Y %H:%M")
    entry = {"DATE": timestamp, "SUBMITTED_AMOUNT": amount * sign}
    reports = normalize_reports(user.get("REPORT", []))
    reports.append(entry)

    users_col.update_one(
        {"USERNAME": username},
        {"$set": {"PROGRESS.OBTAINED_FUND": new_obtained, "REPORT": reports}},
    )
    updated = compute_progress({**progress_doc, "OBTAINED_FUND": new_obtained})
    return jsonify({"success": True, "progress": updated, "entry": entry}), 200


@app.route("/admin/progress/<username>/target", methods=["POST"])
@api_admin_required
def admin_progress_target(username):
    data = request.get_json(silent=True) or {}
    try:
        target = float(data.get("target_amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid numeric target."}), 400
    if target < 0:
        return jsonify({"error": "Target must be zero or greater."}), 400

    result = users_col.update_one({"USERNAME": username}, {"$set": {"PROGRESS.TOTAL_FUND": target}})
    if result.matched_count == 0:
        return jsonify({"error": "User not found."}), 404

    user = users_col.find_one({"USERNAME": username})
    updated = compute_progress(user.get("PROGRESS", {}))
    return jsonify({"success": True, "progress": updated}), 200


# --- Reports (weekly, overall + per member) ------------------------------
def week_key(dt):
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def week_label(dt):
    start = dt - timedelta(days=dt.weekday())
    end = start + timedelta(days=6)
    return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"


@app.route("/admin/reports/weekly", methods=["GET"])
@api_admin_required
def admin_weekly_reports():
    username_filter = request.args.get("username")
    all_users = list(users_col.find({}))
    if username_filter:
        all_users = [u for u in all_users if u.get("USERNAME") == username_filter]

    buckets = {}  # week_key -> {label, total, count, by_user: {username: total}}
    all_transactions = []

    for u in all_users:
        uname = u.get("USERNAME")
        for entry in normalize_reports(u.get("REPORT", [])):
            dt = parse_report_date(entry.get("DATE"))
            amount = report_amount(entry)
            all_transactions.append({
                "username": uname, "date": entry.get("DATE"),
                "amount": amount, "parsed": dt.isoformat() if dt else None,
            })
            if dt is None:
                continue
            key = week_key(dt)
            bucket = buckets.setdefault(key, {
                "week": key, "label": week_label(dt), "total": 0.0, "count": 0, "by_user": {},
            })
            bucket["total"] += amount
            bucket["count"] += 1
            bucket["by_user"][uname] = bucket["by_user"].get(uname, 0.0) + amount

    weeks = sorted(buckets.values(), key=lambda b: b["week"], reverse=True)
    all_transactions.sort(key=lambda t: t["parsed"] or "", reverse=True)

    return jsonify({
        "success": True,
        "weeks": weeks,
        "transactions": all_transactions,
    }), 200


# ---------------------------------------------------------------------------
# Legacy admin routes (server-rendered) — kept for backwards compatibility
# with any existing admin.html template. New work should use /admin/panel.
# ---------------------------------------------------------------------------
def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "username" not in session:
            return "<h3>Unauthorized access. Please login first.</h3>", 401
        user = get_current_user()
        if not user or user.get("ROLE") != "Admin":
            return "<h3>Forbidden. Admins only.</h3>", 403
        return view_func(*args, **kwargs)

    return wrapped


@app.route("/admin", methods=["GET"])
@admin_required
def admin_dashboard():
    all_users = list(users_col.find({}))
    members = [
        {"username": u.get("USERNAME"), "role": u.get("ROLE", "Member"), "is_active": u.get("IS_ACTIVE", True)}
        for u in all_users
    ]
    applications = []
    for u in all_users:
        for app_key, app_data in (u.get("APPLICATION", {}) or {}).items():
            applications.append({
                "username": u.get("USERNAME"), "app_key": app_key, "id": app_data.get("ID"),
                "title": app_data.get("TITLE"), "content": app_data.get("CONTENT"),
                "status": app_data.get("STATUS", "Pending"), "submitted_at": app_data.get("SUBMITTED_AT"),
            })
    progress_list = []
    for u in all_users:
        p = compute_progress(u.get("PROGRESS", {}))
        progress_list.append({"username": u.get("USERNAME"), **p})
    contributions_by_user = []
    for u in all_users:
        items = [{"title": k, "description": v} for k, v in (u.get("CONTRIBUTIONS", {}) or {}).items()]
        contributions_by_user.append({"username": u.get("USERNAME"), "items": items})

    return render_template(
        "admin.html", admin_name=session["username"], members=members, applications=applications,
        progress_list=progress_list, contributions_by_user=contributions_by_user,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050)