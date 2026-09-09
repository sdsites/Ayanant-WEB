import json
import os
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import Lock
from urllib import error as urllib_error
from urllib import parse, request as urllib_request

from PIL import Image, ImageOps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

DATABASE = BASE_DIR / "applications.db"
UPLOAD_FOLDER = BASE_DIR / "uploads" / "resumes"
STATIC_FOLDER = BASE_DIR / "static"
THUMB_FOLDER = STATIC_FOLDER / "_thumbs"
ALLOWED_EXTENSIONS = {"pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
THUMB_SIZE = (720, 520)
THUMB_QUALITY = 78
THUMB_URL_CACHE: dict[str, str] = {}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin@ayanant.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ayanant@123")

CLOUDFLARE_TURNSTILE_SITE_KEY = os.environ.get(
    "CLOUDFLARE_TURNSTILE_SITE_KEY", ""
).strip()
CLOUDFLARE_TURNSTILE_SECRET_KEY = os.environ.get(
    "CLOUDFLARE_TURNSTILE_SECRET_KEY", ""
).strip()


# Simple in-memory rate limiter.
# Limits each visitor IP to 200 non-static requests per 60 seconds.
# Static assets are excluded so images, CSS, JS, and videos do not consume the limit.
RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "200"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_STORE: dict[str, deque[float]] = {}
RATE_LIMIT_LOCK = Lock()


def get_client_ip_for_rate_limit() -> str:
    """Return the best available visitor IP for simple rate limiting."""
    cloudflare_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cloudflare_ip:
        return cloudflare_ip

    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()

    return request.remote_addr or "unknown"


@app.before_request
def apply_simple_rate_limit():
    """Block excessive requests without requiring Redis or external packages."""
    if request.endpoint == "static" or request.path.startswith("/static/"):
        return None

    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    client_ip = get_client_ip_for_rate_limit()

    with RATE_LIMIT_LOCK:
        request_times = RATE_LIMIT_STORE.setdefault(client_ip, deque())

        while request_times and request_times[0] <= cutoff:
            request_times.popleft()

        if len(request_times) >= RATE_LIMIT_REQUESTS:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "Too many requests. Please try again after a minute.",
                    }
                ),
                429,
            )

        request_times.append(now)

        # Keep memory usage controlled by removing old inactive IP entries.
        if len(RATE_LIMIT_STORE) > 5000:
            stale_ips = [
                ip
                for ip, timestamps in RATE_LIMIT_STORE.items()
                if not timestamps or timestamps[-1] <= cutoff
            ]

            for ip in stale_ips[:1000]:
                RATE_LIMIT_STORE.pop(ip, None)

    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def init_storage() -> None:
    """Create upload folder and database tables if they do not exist."""
    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DATABASE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_openings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                department TEXT,
                location TEXT,
                employment_type TEXT,
                description TEXT,
                requirements TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                job_title TEXT,
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
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES job_openings(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                reason TEXT NOT NULL,
                message TEXT NOT NULL,
                ip_address TEXT,
                turnstile_success INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            )       
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS testimonials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote TEXT NOT NULL,
                person_name TEXT NOT NULL,
                role_title TEXT NOT NULL,
                organization TEXT,
                location TEXT,
                tag TEXT,
                ribbon TEXT,
                is_featured INTEGER NOT NULL DEFAULT 0,
                display_order INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            )
            """
        )


        # Safe schema migration for older contact_messages table.
        existing_contact_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(contact_messages)").fetchall()
        }

        required_contact_columns = {
            "status": "TEXT NOT NULL DEFAULT 'new'",
        }

        for column_name, column_type in required_contact_columns.items():
            if column_name not in existing_contact_columns:
                conn.execute(
                    f"ALTER TABLE contact_messages ADD COLUMN {column_name} {column_type}"
                )

        # Safe schema migration for older applications table.
        existing_app_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(applications)").fetchall()
        }

        required_app_columns = {
            "job_id": "INTEGER",
            "job_title": "TEXT",
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

        for column_name, column_type in required_app_columns.items():
            if column_name not in existing_app_columns:
                conn.execute(
                    f"ALTER TABLE applications ADD COLUMN {column_name} {column_type}"
                )

        # Safe schema migration for older job_openings table.
        existing_job_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(job_openings)").fetchall()
        }

        required_job_columns = {
            "title": "TEXT NOT NULL DEFAULT ''",
            "department": "TEXT",
            "location": "TEXT",
            "employment_type": "TEXT",
            "description": "TEXT",
            "requirements": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'open'",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        }

        for column_name, column_type in required_job_columns.items():
            if column_name not in existing_job_columns:
                conn.execute(
                    f"ALTER TABLE job_openings ADD COLUMN {column_name} {column_type}"
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




def _safe_static_image_path(filename: str) -> Path | None:
    """Resolve a static image path safely inside /static."""
    if not filename:
        return None

    clean_name = filename.replace("\\", "/").lstrip("/")
    source_path = (STATIC_FOLDER / clean_name).resolve()

    try:
        source_path.relative_to(STATIC_FOLDER.resolve())
    except ValueError:
        return None

    if source_path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None

    if "_thumbs" in source_path.parts:
        return None

    return source_path


def ensure_thumbnail(filename: str) -> str:
    """
    Create and return a web thumbnail path for a static image.

    The page displays this thumbnail path for faster loading.
    The original image path remains available through data-full-src for modal/full view.
    """
    clean_name = filename.replace("\\", "/").lstrip("/")
    source_path = _safe_static_image_path(clean_name)

    if source_path is None or not source_path.exists():
        return clean_name

    relative_source = source_path.relative_to(STATIC_FOLDER)
    thumb_relative = Path("_thumbs") / relative_source.with_suffix(".webp")
    thumb_path = STATIC_FOLDER / thumb_relative

    try:
        source_mtime = source_path.stat().st_mtime
        thumb_exists = thumb_path.exists()
        thumb_is_fresh = thumb_exists and thumb_path.stat().st_mtime >= source_mtime

        if not thumb_is_fresh:
            thumb_path.parent.mkdir(parents=True, exist_ok=True)

            with Image.open(source_path) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)

                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")

                image.save(
                    thumb_path,
                    "WEBP",
                    quality=THUMB_QUALITY,
                    method=6,
                    optimize=True,
                )

        return thumb_relative.as_posix()
    except Exception:
        return clean_name


def _normalize_static_name(filename: str) -> str:
    """Normalize static file names for URL/cache lookup."""
    return filename.replace("\\", "/").lstrip("/")


def prebuild_unnatfarm_thumbnails() -> None:
    """
    Build UnnatFarm thumbnails once during app startup.

    IMPORTANT:
    This prevents Pillow from resizing/converting images during /unnatfarm page load.
    The page request will only read THUMB_URL_CACHE and return URLs instantly.
    """
    THUMB_URL_CACHE.clear()

    image_paths: list[Path] = []

    # 1) All images inside static/unnat/
    unnat_folder = STATIC_FOLDER / "unnat"
    if unnat_folder.exists():
        image_paths.extend(
            path
            for path in unnat_folder.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )

    # 2) Root-level UnnatFarm success images such as static/unnat11.jpg, etc.
    for pattern in ("unnat*.jpg", "unnat*.jpeg", "unnat*.png", "unnat*.webp"):
        image_paths.extend(
            path
            for path in STATIC_FOLDER.glob(pattern)
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )

    # 3) Logo thumbnail cache support, safe even if template uses original logo directly.
    logo_path = STATIC_FOLDER / "logo.png"
    if logo_path.exists():
        image_paths.append(logo_path)

    seen: set[str] = set()

    for image_path in sorted(image_paths):
        try:
            relative_name = image_path.relative_to(STATIC_FOLDER).as_posix()
        except ValueError:
            continue

        if relative_name in seen:
            continue

        seen.add(relative_name)

        # This is the only place where ensure_thumbnail should create thumbnails.
        THUMB_URL_CACHE[relative_name] = ensure_thumbnail(relative_name)


@app.context_processor
def inject_thumbnail_helpers():
    def thumb_url(filename: str) -> str:
        """
        Return a cached thumbnail URL only.

        IMPORTANT:
        This does NOT generate thumbnails during page rendering.
        If a thumbnail was not prebuilt, it safely returns the original file URL.
        """
        clean_name = _normalize_static_name(filename)
        cached_name = THUMB_URL_CACHE.get(clean_name, clean_name)
        return url_for("static", filename=cached_name)

    return {"thumb_url": thumb_url}

@app.route("/")
@app.route("/home")
def home():
    with get_db_connection() as conn:
        testimonials = conn.execute(
            """
            SELECT
                id, quote, person_name, role_title, organization, location,
                tag, ribbon, is_featured, display_order, status, created_at
            FROM testimonials
            WHERE status = ?
            ORDER BY display_order ASC, id DESC
            """,
            ("open",),
        ).fetchall()

    featured_testimonial = next(
        (testimonial for testimonial in testimonials if testimonial["is_featured"]),
        None,
    )

    normal_testimonials = [
        testimonial for testimonial in testimonials if not testimonial["is_featured"]
    ]

    return render_template(
        "index.html",
        testimonials=testimonials,
        featured_testimonial=featured_testimonial,
        normal_testimonials=normal_testimonials,
    )


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
    return render_template(
        "getintouch.html",
        turnstile_site_key=CLOUDFLARE_TURNSTILE_SITE_KEY,
    )


def verify_turnstile_token(token: str, remote_ip: str | None = None) -> tuple[bool, str]:
    """Verify Cloudflare Turnstile token before accepting a contact message."""
    if not CLOUDFLARE_TURNSTILE_SECRET_KEY:
        return False, "Cloudflare Turnstile is not configured."

    if not token:
        return False, "Please complete the Cloudflare security verification."

    payload = {
        "secret": CLOUDFLARE_TURNSTILE_SECRET_KEY,
        "response": token,
    }

    if remote_ip:
        payload["remoteip"] = remote_ip

    encoded_payload = parse.urlencode(payload).encode("utf-8")
    verification_request = urllib_request.Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        data=encoded_payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib_request.urlopen(verification_request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, json.JSONDecodeError):
        return False, "Cloudflare verification failed. Please try again."

    if result.get("success") is True:
        return True, ""

    return False, "Cloudflare verification failed. Please try again."


@app.route("/contact/submit", methods=["POST"])
def submit_contact():
    # Honeypot: silently accept bots without storing anything.
    if request.form.get("website", "").strip():
        return jsonify(
            {
                "success": True,
                "message": "Thank you for contacting Ayanant. Our team will get back to you soon.",
            }
        )

    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    reason = request.form.get("reason", "").strip()
    message = request.form.get("message", "").strip()
    turnstile_token = request.form.get("cf-turnstile-response", "").strip()

    required_fields = [first_name, last_name, email, reason, message]
    if not all(required_fields):
        return (
            jsonify(
                {
                    "success": False,
                    "message": "Please fill in all required fields before submitting.",
                }
            ),
            400,
        )

    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "Please enter a valid email address.",
                }
            ),
            400,
        )

    turnstile_configured = bool(CLOUDFLARE_TURNSTILE_SECRET_KEY)

    is_verified, verification_message = verify_turnstile_token(
        turnstile_token,
        request.headers.get("CF-Connecting-IP") or request.remote_addr,
    )

    if not is_verified:
        return (
            jsonify(
                {
                    "success": False,
                    "message": verification_message,
                }
            ),
            400,
        )

    turnstile_success = 1 if turnstile_configured and is_verified else 0

    created_at = utc_now().isoformat(timespec="seconds")

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO contact_messages
            (
                first_name, last_name, email, phone, reason, message,
                ip_address, turnstile_success, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                first_name,
                last_name,
                email,
                phone,
                reason,
                message,
                request.headers.get("CF-Connecting-IP") or request.remote_addr,
                turnstile_success,
                created_at,
            ),
        )
        conn.commit()

    return jsonify(
        {
            "success": True,
            "message": "Thank you for contacting Ayanant. Your message has been submitted successfully.",
        }
    )


@app.route("/career")
def career():
    with get_db_connection() as conn:
        job_openings = conn.execute(
            """
            SELECT
                id, title, department, location, employment_type,
                description, requirements, status, created_at
            FROM job_openings
            WHERE status = 'open'
            ORDER BY id DESC
            """
        ).fetchall()

    return render_template("career.html", job_openings=job_openings)


@app.route("/gallery")
def gallery():
    return render_template("gallery.html")


@app.route("/unnatfarm")
def unnatfarm():
    return render_template("unnatfarm.html")


@app.route("/nelocals")
def nelocals():
    return render_template("nelocals.html")


@app.route("/apply", methods=["POST"])
def apply():
    job_id_raw = request.form.get("job_id", "").strip()
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

    if not job_id_raw.isdigit():
        flash("Please select a valid job opening before submitting your application.", "error")
        return redirect(url_for("career") + "#open-roles")

    job_id = int(job_id_raw)

    with get_db_connection() as conn:
        job = conn.execute(
            """
            SELECT id, title
            FROM job_openings
            WHERE id = ? AND status = 'open'
            """,
            (job_id,),
        ).fetchone()

    if job is None:
        flash("This job opening is no longer available.", "error")
        return redirect(url_for("career") + "#open-roles")

    required_fields = [full_name, email, phone, address, city, state, pincode]
    if not all(required_fields):
        flash("Please fill in all required fields.", "error")
        return redirect(url_for("career") + f"#apply-job-{job_id}")

    if resume is None or resume.filename == "":
        flash("Please upload your resume in PDF format.", "error")
        return redirect(url_for("career") + f"#apply-job-{job_id}")

    if not allowed_file(resume.filename):
        flash("Only PDF resumes are allowed.", "error")
        return redirect(url_for("career") + f"#apply-job-{job_id}")

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
                job_id, job_title, full_name, email, phone, portfolio,
                address, city, state, pincode, about, resume_filename,
                role_interest, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job["title"],
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
    return redirect(url_for("career") + "#open-roles")


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
                    id, job_id, job_title, full_name, email, phone, portfolio,
                    address, city, state, pincode, about, resume_filename,
                    role_interest, status, created_at
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
                    id, job_id, job_title, full_name, email, phone, portfolio,
                    address, city, state, pincode, about, resume_filename,
                    role_interest, status, created_at
                FROM applications
                ORDER BY id DESC
                """
            ).fetchall()

        job_openings = conn.execute(
            """
            SELECT
                id, title, department, location, employment_type,
                description, requirements, status, created_at
            FROM job_openings
            ORDER BY
                CASE status
                    WHEN 'open' THEN 1
                    WHEN 'closed' THEN 2
                    ELSE 3
                END,
                id DESC
            """
        ).fetchall()



        contact_total_count = conn.execute(
            "SELECT COUNT(*) FROM contact_messages"
        ).fetchone()[0]

        contact_new_count = conn.execute(
            "SELECT COUNT(*) FROM contact_messages WHERE status = ?",
            ("new",),
        ).fetchone()[0]


        testimonial_total_count = conn.execute(
            "SELECT COUNT(*) FROM testimonials"
        ).fetchone()[0]

        testimonial_open_count = conn.execute(
            "SELECT COUNT(*) FROM testimonials WHERE status = ?",
            ("open",),
        ).fetchone()[0]

    return render_template(
        "admin/dashboard.html",
        applications=applications,
        job_openings=job_openings,
        status_filter=status_filter,
        contact_total_count=contact_total_count,
        contact_new_count=contact_new_count,
        testimonial_total_count=testimonial_total_count,
        testimonial_open_count=testimonial_open_count,
    )



@app.route("/admin/contact-messages")
@admin_required
def admin_contact_messages():
    status_filter = request.args.get("status", "").strip().lower()

    with get_db_connection() as conn:
        if status_filter and status_filter in {"new", "reviewed"}:
            contact_messages = conn.execute(
                """
                SELECT
                    id, first_name, last_name, email, phone, reason, message,
                    ip_address, turnstile_success, status, created_at
                FROM contact_messages
                WHERE status = ?
                ORDER BY id DESC
                """,
                (status_filter,),
            ).fetchall()
        else:
            contact_messages = conn.execute(
                """
                SELECT
                    id, first_name, last_name, email, phone, reason, message,
                    ip_address, turnstile_success, status, created_at
                FROM contact_messages
                ORDER BY id DESC
                """
            ).fetchall()

        contact_total_count = conn.execute(
            "SELECT COUNT(*) FROM contact_messages"
        ).fetchone()[0]

        contact_new_count = conn.execute(
            "SELECT COUNT(*) FROM contact_messages WHERE status = ?",
            ("new",),
        ).fetchone()[0]

        contact_reviewed_count = conn.execute(
            "SELECT COUNT(*) FROM contact_messages WHERE status = ?",
            ("reviewed",),
        ).fetchone()[0]

    return render_template(
        "admin/contact_messages.html",
        contact_messages=contact_messages,
        status_filter=status_filter,
        contact_total_count=contact_total_count,
        contact_new_count=contact_new_count,
        contact_reviewed_count=contact_reviewed_count,
    )


@app.route("/admin/contact-message/<int:id>/review", methods=["POST"])
@admin_required
def review_contact_message(id: int):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE contact_messages SET status = ? WHERE id = ?",
            ("reviewed", id),
        )
        conn.commit()

    flash("Contact message marked as reviewed.", "success")
    return redirect(request.referrer or url_for("admin_contact_messages"))


@app.route("/admin/contact-message/<int:id>/new", methods=["POST"])
@admin_required
def mark_contact_message_new(id: int):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE contact_messages SET status = ? WHERE id = ?",
            ("new", id),
        )
        conn.commit()

    flash("Contact message marked as new.", "success")
    return redirect(request.referrer or url_for("admin_contact_messages"))


@app.route("/admin/contact-message/<int:id>/delete", methods=["POST"])
@admin_required
def delete_contact_message(id: int):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM contact_messages WHERE id = ?", (id,))
        conn.commit()

    flash("Contact message deleted successfully.", "success")
    return redirect(url_for("admin_contact_messages"))


@app.route("/admin/testimonials", methods=["GET", "POST"])
@admin_required
def admin_testimonials():
    if request.method == "POST":
        quote = request.form.get("quote", "").strip()
        person_name = request.form.get("person_name", "").strip()
        role_title = request.form.get("role_title", "").strip()
        organization = request.form.get("organization", "").strip()
        location = request.form.get("location", "").strip()
        tag = request.form.get("tag", "").strip()
        ribbon = request.form.get("ribbon", "").strip()
        status = request.form.get("status", "open").strip().lower()

        try:
            display_order = int(request.form.get("display_order", "0").strip() or 0)
        except ValueError:
            display_order = 0

        is_featured = 1 if request.form.get("is_featured") == "on" else 0

        if status not in {"open", "closed"}:
            status = "open"

        if not quote or not person_name or not role_title:
            flash("Quote, name/title, and role/details are required.", "error")
            return redirect(url_for("admin_testimonials"))

        created_at = utc_now().isoformat(timespec="seconds")

        with get_db_connection() as conn:
            if is_featured:
                conn.execute("UPDATE testimonials SET is_featured = 0")

            conn.execute(
                """
                INSERT INTO testimonials
                (
                    quote, person_name, role_title, organization, location,
                    tag, ribbon, is_featured, display_order, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quote,
                    person_name,
                    role_title,
                    organization,
                    location,
                    tag,
                    ribbon,
                    is_featured,
                    display_order,
                    status,
                    created_at,
                ),
            )
            conn.commit()

        flash("Testimonial added successfully.", "success")
        return redirect(url_for("admin_testimonials"))

    with get_db_connection() as conn:
        testimonials = conn.execute(
            """
            SELECT
                id, quote, person_name, role_title, organization, location,
                tag, ribbon, is_featured, display_order, status, created_at
            FROM testimonials
            ORDER BY is_featured DESC, display_order ASC, id DESC
            """
        ).fetchall()

        testimonial_total_count = conn.execute(
            "SELECT COUNT(*) FROM testimonials"
        ).fetchone()[0]

        testimonial_open_count = conn.execute(
            "SELECT COUNT(*) FROM testimonials WHERE status = ?",
            ("open",),
        ).fetchone()[0]

        testimonial_closed_count = conn.execute(
            "SELECT COUNT(*) FROM testimonials WHERE status = ?",
            ("closed",),
        ).fetchone()[0]

    return render_template(
        "admin/testimonials.html",
        testimonials=testimonials,
        testimonial_total_count=testimonial_total_count,
        testimonial_open_count=testimonial_open_count,
        testimonial_closed_count=testimonial_closed_count,
    )


@app.route("/admin/testimonial/<int:id>/update", methods=["POST"])
@admin_required
def update_testimonial(id: int):
    quote = request.form.get("quote", "").strip()
    person_name = request.form.get("person_name", "").strip()
    role_title = request.form.get("role_title", "").strip()
    organization = request.form.get("organization", "").strip()
    location = request.form.get("location", "").strip()
    tag = request.form.get("tag", "").strip()
    ribbon = request.form.get("ribbon", "").strip()
    status = request.form.get("status", "open").strip().lower()

    try:
        display_order = int(request.form.get("display_order", "0").strip() or 0)
    except ValueError:
        display_order = 0

    is_featured = 1 if request.form.get("is_featured") == "on" else 0

    if status not in {"open", "closed"}:
        status = "open"

    if not quote or not person_name or not role_title:
        flash("Quote, name/title, and role/details are required.", "error")
        return redirect(url_for("admin_testimonials"))

    with get_db_connection() as conn:
        if is_featured:
            conn.execute(
                "UPDATE testimonials SET is_featured = 0 WHERE id != ?",
                (id,),
            )

        conn.execute(
            """
            UPDATE testimonials
            SET
                quote = ?,
                person_name = ?,
                role_title = ?,
                organization = ?,
                location = ?,
                tag = ?,
                ribbon = ?,
                is_featured = ?,
                display_order = ?,
                status = ?
            WHERE id = ?
            """,
            (
                quote,
                person_name,
                role_title,
                organization,
                location,
                tag,
                ribbon,
                is_featured,
                display_order,
                status,
                id,
            ),
        )
        conn.commit()

    flash("Testimonial updated successfully.", "success")
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonial/<int:id>/toggle", methods=["POST"])
@admin_required
def toggle_testimonial(id: int):
    with get_db_connection() as conn:
        testimonial = conn.execute(
            "SELECT status FROM testimonials WHERE id = ?",
            (id,),
        ).fetchone()

        if testimonial:
            new_status = "closed" if testimonial["status"] == "open" else "open"
            conn.execute(
                "UPDATE testimonials SET status = ? WHERE id = ?",
                (new_status, id),
            )
            conn.commit()

    flash("Testimonial status updated.", "success")
    return redirect(url_for("admin_testimonials"))


@app.route("/admin/testimonial/<int:id>/delete", methods=["POST"])
@admin_required
def delete_testimonial(id: int):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM testimonials WHERE id = ?", (id,))
        conn.commit()

    flash("Testimonial deleted successfully.", "success")
    return redirect(url_for("admin_testimonials"))

@app.route("/admin/jobs/create", methods=["GET", "POST"])
@admin_required
def create_job_opening():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        department = request.form.get("department", "").strip()
        location = request.form.get("location", "").strip()
        employment_type = request.form.get("employment_type", "").strip()
        description = request.form.get("description", "").strip()
        requirements = request.form.get("requirements", "").strip()

        if not title:
            flash("Job title is required.", "error")
            return redirect(url_for("create_job_opening"))

        created_at = utc_now().isoformat(timespec="seconds")

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO job_openings
                (
                    title, department, location, employment_type,
                    description, requirements, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    department,
                    location,
                    employment_type,
                    description,
                    requirements,
                    "open",
                    created_at,
                ),
            )
            conn.commit()

        flash("Job opening posted successfully.", "success")
        return redirect(url_for("admin_dashboard") + "#job-openings")

    return render_template("admin/job_form.html")


@app.route("/admin/job/<int:id>/close")
@admin_required
def close_job_opening(id: int):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE job_openings SET status = ? WHERE id = ?",
            ("closed", id),
        )
        conn.commit()

    flash("Job opening closed successfully.", "success")
    return redirect(url_for("admin_dashboard") + "#job-openings")


@app.route("/admin/job/<int:id>/reopen")
@admin_required
def reopen_job_opening(id: int):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE job_openings SET status = ? WHERE id = ?",
            ("open", id),
        )
        conn.commit()

    flash("Job opening reopened successfully.", "success")
    return redirect(url_for("admin_dashboard") + "#job-openings")


@app.route("/admin/job/<int:id>/delete", methods=["POST"])
@admin_required
def delete_job_opening(id: int):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM job_openings WHERE id = ?", (id,))
        conn.commit()

    flash("Job opening removed successfully.", "success")
    return redirect(url_for("admin_dashboard") + "#job-openings")


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
prebuild_unnatfarm_thumbnails()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)