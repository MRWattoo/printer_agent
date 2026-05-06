"""
printer_app/app.py
Flask web application for managing IP-based ESC/POS printers.
"""

import os
import sqlite3
import logging
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

from .print_agent import agent_manager, print_test, PrinterNotReachableError, PrinterHardwareError, check_printer_connectivity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"

_DATA_DIR = Path(os.environ.get("PRINTER_APP_DATA", Path.home() / ".printer_app"))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = _DATA_DIR / "printers.db"

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    template_folder=str(_TEMPLATES_DIR),
    static_folder=str(_STATIC_DIR),
)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-printer-key")


# ---------------------------------------------------------------------------
# Auth Decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            return "Forbidden: Admin access required", 403
        return f(*args, **kwargs)
    return decorated_function


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        # 1. Settings table (Global configuration)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                odoo_url     TEXT    NOT NULL DEFAULT '',
                api_key      TEXT    NOT NULL DEFAULT '',
                company_id   INTEGER NOT NULL DEFAULT 1,
                allowed_ip   TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        # Migration: Add allowed_ip column if it doesn't exist
        try:
            conn.execute("ALTER TABLE settings ADD COLUMN allowed_ip TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        # Ensure initial row exists
        if not conn.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
            conn.execute("INSERT INTO settings (id, odoo_url, api_key, company_id, allowed_ip) VALUES (1, '', '', 1, '')")

        # 2. Printers table (Simplified)
        # Note: We keep odoo_url, api_key, company_id for backward compatibility 
        # during migration or handle it via a new table if preferred. 
        # For a clean approach, we'll try to rename the old table and recreate.
        
        # Check if columns still exist in printers table
        cursor = conn.execute("PRAGMA table_info(printers)")
        columns_info = cursor.fetchall()
        columns = [row[1] for row in columns_info]
        
        # Check if 'ip' is already unique
        is_ip_unique = any(row[1] == 'ip' and row[5] == 1 for row in columns_info) # This isn't reliable for all sqlite versions
        # Better to check if the table needs recreation based on missing unique constraint or extra columns
        
        if "odoo_url" in columns or not is_ip_unique:
            # Migration step: Move values from first printer to global settings if settings are empty
            settings = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
            if not settings["odoo_url"] or not settings["api_key"]:
                # Try to get data from old table if it exists, or current table before migration
                table_to_read = "printers" if "odoo_url" in columns else "printers_old"
                try:
                    first_printer = conn.execute(f"SELECT * FROM {table_to_read} LIMIT 1").fetchone()
                    if first_printer:
                        conn.execute(
                            "UPDATE settings SET odoo_url=?, api_key=?, company_id=? WHERE id=1",
                            (first_printer["odoo_url"], first_printer["api_key"], first_printer["company_id"])
                        )
                except:
                    pass

            # Recreate printers table with UNIQUE IP constraint
            conn.execute("DROP TABLE IF EXISTS printers_old")
            conn.execute("ALTER TABLE printers RENAME TO printers_old")
            conn.execute(
                """
                CREATE TABLE printers (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT    NOT NULL,
                    ip         TEXT    NOT NULL UNIQUE,
                    enabled    INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            # Use INSERT OR IGNORE to handle existing duplicates during migration
            conn.execute("INSERT OR IGNORE INTO printers (id, name, ip, enabled) SELECT id, name, ip, enabled FROM printers_old")
            conn.execute("DROP TABLE printers_old")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE,
                name          TEXT,
                password_hash TEXT    NOT NULL,
                role          TEXT    NOT NULL DEFAULT 'user'
            )
            """
        )

        # Migration: Add name column if it doesn't exist
        try:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Seed default admin
        admin = conn.execute("SELECT * FROM users WHERE username='wattoo'").fetchone()
        if not admin:
            # Password: 3r6&&$u63r!or##
            hashed = generate_password_hash("3r6&&$u63r!or##")
            conn.execute(
                "INSERT INTO users (username, name, password_hash, role) VALUES (?, ?, ?, ?)",
                ("wattoo", "Mohsan Raza Wattoo", hashed, "admin")
            )
            conn.commit()


def get_settings() -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
    return dict(row) if row else {"odoo_url": "", "api_key": "", "company_id": 1, "allowed_ip": ""}


def update_settings(odoo_url: str, api_key: str, company_id: int, allowed_ip: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE settings SET odoo_url=?, api_key=?, company_id=?, allowed_ip=? WHERE id=1",
            (odoo_url, api_key, company_id, allowed_ip)
        )
        conn.commit()

def row_to_dict(row) -> dict:
    return dict(row)


def get_all_printers() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM printers ORDER BY id").fetchall()
    return [row_to_dict(r) for r in rows]


def get_printer(printer_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM printers WHERE id=?", (printer_id,)).fetchone()
    return row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            return redirect(url_for("index"))
        
        return render_template("login.html", error="Invalid username or password")
    
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def users_management():
    if request.method == "POST":
        username = request.form.get("username").strip()
        name = request.form.get("name", "").strip()
        password = request.form.get("password")
        role = request.form.get("role", "user")

        if not username or not password:
            return render_template("users.html", error="Username and password required", users=get_all_users(session.get("username")))

        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO users (username, name, password_hash, role) VALUES (?, ?, ?, ?)",
                    (username, name, generate_password_hash(password), role)
                )
                conn.commit()
        except sqlite3.IntegrityError:
            return render_template("users.html", error="Username already exists", users=get_all_users(session.get("username")))            
        return redirect(url_for("users_management"))

    return render_template("users.html", users=get_all_users(session.get("username")), current_username=session.get("username"))

@app.route("/users/delete/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id: int):
    with get_db() as conn:
        user = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if user:
            if user["username"] == "wattoo":
                flash("Default user 'wattoo' cannot be deleted.", "error")
            else:
                conn.execute("DELETE FROM users WHERE id=?", (user_id,))
                conn.commit()
                flash(f"User '{user['username']}' deleted.", "success")
    return redirect(url_for("users_management"))


@app.route("/users/change_password/<int:user_id>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_change_password(user_id: int):
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    if not user:
        return "User not found", 404

    # Security: Only 'wattoo' can change 'wattoo''s password.
    # Other admins cannot change 'wattoo''s password.
    if user["username"] == "wattoo" and session.get("username") != "wattoo":
        flash("Only the system user can change their own password.", "error")
        return redirect(url_for("users_management"))

    if request.method == "POST":
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")
        
        if not new_password:
            return render_template("change_password.html", user=row_to_dict(user), error="Password is required")
        
        if new_password != confirm_password:
            return render_template("change_password.html", user=row_to_dict(user), error="New passwords do not match")
        
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(new_password), user_id)
            )
            conn.commit()
        
        flash(f"Password changed for user {user['username']}", "success")
        return redirect(url_for("users_management"))
    
    return render_template("change_password.html", user=row_to_dict(user))


@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_own_password():
    user_id = session.get('user_id')
    
    if request.method == "POST":
        current_password = request.form.get("current_password")
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")
        
        if not current_password or not new_password or not confirm_password:
            return render_template("change_password.html", error="All fields are required", own_password=True)
        
        if new_password != confirm_password:
            return render_template("change_password.html", error="New passwords do not match", own_password=True)
            
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        
        if not user or not check_password_hash(user["password_hash"], current_password):
            return render_template("change_password.html", error="Current password is incorrect", own_password=True)
        
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(new_password), user_id)
            )
            conn.commit()
        
        flash("Your password has been changed successfully", "success")
        return redirect(url_for("index"))
    
    return render_template("change_password.html", own_password=True)

def get_all_users(viewer_username=None):
    with get_db() as conn:
        if viewer_username == "wattoo":
            return conn.execute("SELECT id, username, name, role FROM users").fetchall()
        else:
            return conn.execute("SELECT id, username, name, role FROM users WHERE username != 'wattoo'").fetchall()


def sync_printers_from_odoo():
    """
    Fetch printers from Odoo and update local database.
    """
    settings = get_settings()
    if not settings["odoo_url"] or not settings["api_key"]:
        return False, "Odoo URL or API Key not configured"
    
    try:
        import requests
        url = f"{settings['odoo_url'].rstrip('/')}/odoo_pos/printers"
        headers = {"Authorization": f"Bearer {settings['api_key']}"}
        # Odoo route expects JSON for auth='none' type='json'
        resp = requests.post(
            url, 
            json={"params": {"company_id": settings["company_id"]}}, 
            headers=headers, 
            timeout=10
        )
        
        if resp.status_code == 200:
            data = resp.json()
            if "error" in data:
                return False, f"Odoo Error: {data['error'].get('message', 'Unknown error')}"
            
            result_wrapper = data.get("result", {})
            if result_wrapper.get("status") != "success":
                return False, f"Odoo API Error: {result_wrapper.get('error', 'Unknown')}"
            
            printers_data = result_wrapper.get("result", [])
            
            with get_db() as conn:
                existing = {p["ip"]: p["id"] for p in get_all_printers()}
                
                for p_data in printers_data:
                    name = p_data.get("name")
                    ip = p_data.get("ip")
                    if not ip: continue
                    
                    if ip in existing:
                        conn.execute("UPDATE printers SET name=? WHERE ip=?", (name, ip))
                    else:
                        conn.execute("INSERT INTO printers (name, ip, enabled) VALUES (?, ?, 1)", (name, ip))
                conn.commit()
            
            # Restart all enabled printers to ensure they use latest settings
            start_all_enabled()
            return True, f"Successfully synced {len(printers_data)} printers from Odoo"
        else:
            return False, f"Odoo returned HTTP {resp.status_code}"
    except Exception as e:
        logging.error("Sync failed: %s", e)
        return False, f"Sync failed: {str(e)}"


@app.before_request
def restrict_ip():
    settings = get_settings()
    allowed = settings.get("allowed_ip")
    if allowed and allowed.strip():
        # Allow static files and the login page so users can actually log in 
        # (or at least see why they are blocked if we add a custom page)
        if request.endpoint not in ('login', 'static', 'api_status'):
            client_ip = request.remote_addr
            # Handle X-Forwarded-For if behind a proxy
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
            
            if client_ip != allowed.strip():
                from flask import abort
                logging.warning("Blocked access attempt from unauthorized IP: %s", client_ip)
                abort(403, description=f"Access denied from IP: {client_ip}")


# ---------------------------------------------------------------------------
# Routes — Master Configuration
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def master_settings():
    if request.method == "POST":
        odoo_url = request.form.get("odoo_url", "").strip()
        api_key = request.form.get("api_key", "").strip()
        company_id = int(request.form.get("company_id", 1))
        allowed_ip = request.form.get("allowed_ip", "").strip()

        update_settings(odoo_url, api_key, company_id, allowed_ip)
        
        # Restart all enabled printers to apply new settings
        settings = get_settings()
        for p in get_all_printers():
            if p["enabled"]:
                agent_manager.restart(p, settings)
        
        flash("Global settings updated and printers restarted.", "success")
        return redirect(url_for("index"))

    settings = get_settings()
    return render_template("settings.html", settings=settings)


@app.route("/sync", methods=["POST"])
@login_required
@admin_required
def sync_printers():
    success, message = sync_printers_from_odoo()
    if success:
        flash(message, "success")
    else:
        flash(message, "error")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes — Printer Management
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    printers = get_all_printers()
    statuses = {p["id"]: agent_manager.is_alive(p["id"]) for p in printers}
    return render_template("index.html",
                           printers=printers,
                           statuses=statuses,
                           role=session.get('role'),
                           username=session.get('username'),
                           display_name=session.get('name') or session.get('username'))

@app.route("/add", methods=["GET", "POST"])
@login_required
def add_printer():
    # Both Admin and User can add printers
    if request.method == "POST":
        data = request.form
        try:
            with get_db() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO printers (name, ip, enabled)
                    VALUES (?, ?, ?)
                    """,
                    (
                        data["name"].strip(),
                        data["ip"].strip(),
                        1,
                    ),
                )
                conn.commit()
                printer_id = cur.lastrowid

            printer = get_printer(printer_id)
            agent_manager.start(printer, get_settings())
            return redirect(url_for("index"))
        except sqlite3.IntegrityError:
            flash(f"Error: A printer with IP '{data['ip']}' already exists.", "error")
            return render_template("form.html", printer=None, title="Add Printer")

    return render_template("form.html", printer=None, title="Add Printer")


@app.route("/edit/<int:printer_id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_printer(printer_id: int):
    # Only Admin can edit printers
    printer = get_printer(printer_id)
    if not printer:
        return "Printer not found", 404

    if request.method == "POST":
        data = request.form
        try:
            with get_db() as conn:
                conn.execute(
                    """
                    UPDATE printers
                    SET name=?, ip=?, enabled=?
                    WHERE id=?
                    """,
                    (
                        data["name"].strip(),
                        data["ip"].strip(),
                        int(data.get("enabled", 1)),
                        printer_id,
                    ),
                )
                conn.commit()

            updated = get_printer(printer_id)
            if updated["enabled"]:
                agent_manager.restart(updated, get_settings())
            else:
                agent_manager.stop(printer_id)

            return redirect(url_for("index"))
        except sqlite3.IntegrityError:
            flash(f"Error: IP address '{data['ip']}' is already used by another printer.", "error")
            return render_template("form.html", printer=printer, title="Edit Printer")

    return render_template("form.html", printer=printer, title="Edit Printer")


@app.route("/delete/<int:printer_id>", methods=["POST"])
@login_required
@admin_required
def delete_printer(printer_id: int):
    # Only main admin (wattoo) can delete printers
    if session.get('username') != 'wattoo':
        flash("Only the main admin can delete printers", "error")
        return redirect(url_for("index"))
    
    agent_manager.stop(printer_id)
    with get_db() as conn:
        conn.execute("DELETE FROM printers WHERE id=?", (printer_id,))
        conn.commit()
    return redirect(url_for("index"))


@app.route("/toggle/<int:printer_id>", methods=["POST"])
@login_required
def toggle_printer(printer_id: int):
    # Both Admin and User can toggle (archive) printers
    printer = get_printer(printer_id)
    if not printer:
        return jsonify({"error": "not found"}), 404

    new_state = 0 if printer["enabled"] else 1
    with get_db() as conn:
        conn.execute("UPDATE printers SET enabled=? WHERE id=?", (new_state, printer_id))
        conn.commit()

    updated = get_printer(printer_id)
    if updated["enabled"]:
        agent_manager.start(updated, get_settings())
    else:
        agent_manager.stop(printer_id)

    return redirect(url_for("index"))


@app.route("/test_print/<int:printer_id>", methods=["POST"])
@login_required
def test_print(printer_id: int):
    # Both Admin and User can send test prints
    printer = get_printer(printer_id)
    if not printer:
        return jsonify({"error": "not found"}), 404

    try:
        print_test(printer["ip"])
        flash(f"Test print sent successfully to {printer['name']}", "success")
    except PrinterNotReachableError as e:
        flash(f"Test print failed - printer not reachable: {e}", "error")
    except PrinterHardwareError as e:
        flash(f"Test print failed - hardware error: {e}", "error")
    except Exception as e:
        flash(f"Test print failed - unexpected error: {e}", "error")

    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# API — status endpoint
# ---------------------------------------------------------------------------

@app.route("/api/status")
@login_required
def api_status():
    printers = get_all_printers()
    return jsonify(
        [
            {
                "id": p["id"],
                "name": p["name"],
                "ip": p["ip"],
                "enabled": bool(p["enabled"]),
                "running": agent_manager.is_alive(p["id"]),
                # connected now means verified as a printer
                "connected": check_printer_connectivity(p["ip"]) if p["enabled"] else False,
            }
            for p in printers
        ]
    )


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def start_all_enabled():
    settings = get_settings()
    for p in get_all_printers():
        if p["enabled"]:
            agent_manager.start(p, settings)


def main():
    """Entry point called by the `printer-app` console script."""
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        from importlib.metadata import version, PackageNotFoundError
        try:
            print(version("printer-app"))
        except PackageNotFoundError:
            print("unknown")
        return

    port = int(os.environ.get("PRINTER_APP_PORT", 5000))
    host = os.environ.get("PRINTER_APP_HOST", "0.0.0.0")
    init_db()
    start_all_enabled()
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
