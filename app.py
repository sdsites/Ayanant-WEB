import os
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "applications.db"
UPLOAD_FOLDER = BASE_DIR / "uploads" / "resumes"
ALLOWED_EXTENSIONS = {"pdf"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin@ayanant.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ayanant@123")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def init_storage() -> None:
    """Create upload folder and database table if they do not exist."""
    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                portfolio TEXT,
                address TEXT NOT NULL,
                city TEXT NOT NULL,
                state TEXT NOT NULL,
                pincode TEXT NOT NULL,
                about TEXT,
                resume_filename TEXT NOT NULL,
                role_interest TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            )
            """
        )

        # Safe schema migration for older databases.
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(applications)").fetchall()
        }

        required_columns = {
            "full_name": "TEXT NOT NULL DEFAULT ''",
            "email": "TEXT NOT NULL DEFAULT ''",
            "phone": "TEXT NOT NULL DEFAULT ''",
            "portfolio": "TEXT",
            "address": "TEXT NOT NULL DEFAULT ''",
            "city": "TEXT NOT NULL DEFAULT ''",
            "state": "TEXT NOT NULL DEFAULT ''",
            "pincode": "TEXT NOT NULL DEFAULT ''",
            "about": "TEXT",
            "resume_filename": "TEXT NOT NULL DEFAULT ''",
            "role_interest": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'new'",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        }

        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                conn.execute(
                    f"ALTER TABLE applications ADD COLUMN {column_name} {column_type}"
                )

        conn.commit()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def admin_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("Please log in to access the admin panel.", "error")
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)

    return wrapped_view


@app.route("/")
@app.route("/home")
def home():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about_us.html")


@app.route("/team")
def team():
    return render_template("team.html")


@app.route("/products")
def products():
    return render_template("products.html")


@app.route("/contact")
def contact():
    return render_template("getintouch.html")


@app.route("/career")
def career():
    return render_template("career.html")


@app.route("/gallery")
def gallery():
    return render_template("gallery.html")


@app.route("/unnatfarm")
def unnatfarm():
    return render_template("unnatfarm.html")


@app.route("/apply", methods=["POST"])
def apply():
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    portfolio = request.form.get("portfolio", "").strip()
    address = request.form.get("address", "").strip()
    city = request.form.get("city", "").strip()
    state = request.form.get("state", "").strip()
    pincode = request.form.get("pincode", "").strip()
    about = request.form.get("about", "").strip()
    role_interest = request.form.get("role_interest", "").strip()
    resume = request.files.get("resume")

    required_fields = [full_name, email, phone, address, city, state, pincode]
    if not all(required_fields):
        flash("Please fill in all required fields.", "error")
        return redirect(url_for("career") + "#apply")

    if resume is None or resume.filename == "":
        flash("Please upload your resume in PDF format.", "error")
        return redirect(url_for("career") + "#apply")

    if not allowed_file(resume.filename):
        flash("Only PDF resumes are allowed.", "error")
        return redirect(url_for("career") + "#apply")

    original_name = secure_filename(resume.filename)
    unique_name = f"{utc_now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}_{original_name}"
    save_path = UPLOAD_FOLDER / unique_name
    resume.save(save_path)

    created_at = utc_now().isoformat(timespec="seconds")

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO applications
            (
                full_name, email, phone, portfolio, address, city, state, pincode,
                about, resume_filename, role_interest, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                full_name,
                email,
                phone,
                portfolio,
                address,
                city,
                state,
                pincode,
                about,
                unique_name,
                role_interest,
                "new",
                created_at,
            ),
        )
        conn.commit()

    flash("Your application has been submitted successfully.", "success")
    return redirect(url_for("career") + "#apply")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            flash("Login successful.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin username or password.", "error")

    return render_template("login.html")


@app.route("/admin/logout")
@admin_required
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
@app.route("/admin/applications")
@admin_required
def admin_dashboard():
    status_filter = request.args.get("status", "").strip().lower()

    with get_db_connection() as conn:
        if status_filter and status_filter in {"new", "shortlisted", "rejected", "reviewed"}:
            applications = conn.execute(
                """
                SELECT
                    id, full_name, email, phone, portfolio, address, city, state,
                    pincode, about, resume_filename, role_interest, status, created_at
                FROM applications
                WHERE status = ?
                ORDER BY id DESC
                """,
                (status_filter,),
            ).fetchall()
        else:
            applications = conn.execute(
                """
                SELECT
                    id, full_name, email, phone, portfolio, address, city, state,
                    pincode, about, resume_filename, role_interest, status, created_at
                FROM applications
                ORDER BY id DESC
                """
            ).fetchall()

    return render_template(
        "admin/dashboard.html",
        applications=applications,
        status_filter=status_filter,
    )


@app.route("/admin/resume/<path:filename>")
@admin_required
def view_resume(filename):
    file_path = UPLOAD_FOLDER / filename
    if not file_path.exists():
        flash("Resume file not found.", "error")
        return redirect(url_for("admin_dashboard"))
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/admin/application/<int:id>/shortlist", endpoint="shortlist_application")
@admin_required
def shortlist(id: int):
    with get_db_connection() as conn:
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", ("shortlisted", id))
        conn.commit()

    flash("Candidate shortlisted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/application/<int:id>/reject", endpoint="reject_application")
@admin_required
def reject(id: int):
    with get_db_connection() as conn:
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", ("rejected", id))
        conn.commit()

    flash("Candidate marked as rejected.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/application/<int:id>/review", endpoint="review_application")
@admin_required
def review(id: int):
    with get_db_connection() as conn:
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", ("reviewed", id))
        conn.commit()

    flash("Candidate marked as reviewed.", "success")
    return redirect(url_for("admin_dashboard"))


init_storage()

if __name__ == "__main__":
    app.run(debug=True)
