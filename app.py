import os
import csv
import io
import json
import hashlib
import logging
import re
import secrets
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from contextlib import contextmanager

from flask import (
    Flask, jsonify, request, render_template, redirect,
    url_for, flash, Response, session, g
)
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from psycopg2 import OperationalError

load_dotenv()

APP_ENV = (os.environ.get("APP_ENV") or "development").strip().lower()
IS_PROD = APP_ENV == "production"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("fiverrflow")

# ── Secret key: never fall back to a known value in production ────────────────
_PLACEHOLDER_KEYS = {"", "change-me-in-production", "dev-secret-key"}
_secret = (os.environ.get("SECRET_KEY") or "").strip()
if _secret in _PLACEHOLDER_KEYS:
    if IS_PROD:
        raise RuntimeError(
            "SECRET_KEY is unset or still the placeholder value. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\" "
            "and set it in the environment before starting in production."
        )
    _secret = "dev-only-insecure-key"
    log.warning("SECRET_KEY not set — using an insecure development key.")

app = Flask(__name__)
app.secret_key = _secret
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # 5 MB cap on CSV uploads
    # Static files are fingerprinted by static_url() below, so they can be
    # cached hard. Without this Flask sends no-cache and the extraction of
    # CSS/JS out of base.html buys nothing on repeat visits.
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=365) if IS_PROD else timedelta(0),
)

COMMISSION_RATE = float(os.environ.get("COMMISSION_RATE", "0.8"))

csrf = CSRFProtect(app)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()

# ── DB (psycopg2 + Supabase Postgres) ─────────────────────────────────────────
def _normalize_database_url(url: str) -> str:
    """Ensure sslmode and reject obviously broken hosts."""
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "In Render → Environment, set DATABASE_URL to your "
            "Supabase pooler URI (port 6543), e.g. "
            "postgresql://postgres.REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres?sslmode=require"
        )
    # Common mistake: port glued into the host as "...@6543@aws-0-region...".
    # More than one '@' also usually means the password was not URL-encoded.
    if url.count("@") > 1 and re.search(r"@\d+@aws-", url):
        raise RuntimeError(
            "DATABASE_URL has more than one '@'. "
            "URL-encode special characters in the password (@ → %40). "
            "Host should look like: aws-0-ap-southeast-1.pooler.supabase.com"
        )
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


_pool = None


def _get_pool():
    """Lazily build a process-wide connection pool.

    Previously every query opened its own TLS connection to Postgres, so a single
    dashboard render cost ~7 handshakes. One pool per worker process replaces that.
    """
    global _pool
    if _pool is None:
        from psycopg2.extras import RealDictCursor
        from psycopg2.pool import ThreadedConnectionPool

        url = _normalize_database_url(DATABASE_URL)
        maxconn = int(os.environ.get("DB_POOL_MAX", "8"))
        try:
            _pool = ThreadedConnectionPool(
                minconn=1,
                maxconn=maxconn,
                dsn=url,
                cursor_factory=RealDictCursor,
                connect_timeout=15,
            )
        except Exception as e:
            raise RuntimeError(
                f"Database connection failed: {e}\n\n"
                f"Check DATABASE_URL. Expected form:\n"
                f"postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres?sslmode=require\n"
                f"Password special chars must be URL-encoded. Port must be after the hostname."
            ) from e
        log.info("DB pool initialised (maxconn=%s)", maxconn)
    return _pool


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        pool.putconn(conn)


def close_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
            log.info("DB pool closed")
        except Exception:
            pass
        _pool = None


import atexit
atexit.register(close_pool)


def _db_retry(fn):
    """Render free instances sleep; pooled TCP connections go stale and the
    first query after a wake hits a dead socket. Close the pool and retry once.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OperationalError:
            log.warning(
                "OperationalError in %s — rebuilding pool and retrying once",
                fn.__name__, exc_info=True,
            )
            close_pool()
            return fn(*args, **kwargs)
    return wrapper


@_db_retry
def q(sql, args=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return [dict(r) for r in cur.fetchall()]


def q1(sql, args=None):
    rows = q(sql, args)
    return rows[0] if rows else None


@_db_retry
def run(sql, args=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.rowcount


def scalar(sql, args=None):
    row = q1(sql, args)
    if not row:
        return None
    return list(row.values())[0]


@_db_retry
def insert_returning(sql, args=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            row = cur.fetchone()
            return dict(row) if row else None


# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_password(password):
    """Hash with Werkzeug's default (scrypt)."""
    return generate_password_hash(password)


def _is_legacy_hash(stored):
    """Legacy format is 'salt:sha256hex' — 32-char hex salt, 64-char hex digest.

    Werkzeug hashes contain '$' separators, so the two are unambiguous.
    """
    return "$" not in (stored or "") and (stored or "").count(":") == 1


def verify_password(password, stored):
    """Accept both the legacy sha256 scheme and Werkzeug hashes.

    The live database still holds legacy hashes; they keep working, and
    login() transparently upgrades them on next successful sign-in.
    """
    if not stored:
        return False
    if _is_legacy_hash(stored):
        try:
            salt, h = stored.split(":", 1)
            return hashlib.sha256((salt + password).encode()).hexdigest() == h
        except Exception:
            return False
    try:
        return check_password_hash(stored, password)
    except Exception:
        return False


def parse_date(val):
    if not val:
        return None
    if isinstance(val, (date, datetime)):
        return val if isinstance(val, date) and not isinstance(val, datetime) else val.date()
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def parse_money(val):
    """Parse amounts like '$6,000.00', '6,000', '6000.50', '' → Decimal.

    Money stays in Decimal (matching the DB's NUMERIC columns) so it never
    passes through binary floats. Display formatting lives in the `money`
    filter, which handles Decimal via float().
    """
    if val is None or val == "":
        return Decimal("0")
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    s = str(val).strip()
    if not s:
        return Decimal("0")
    # Remove currency symbols, spaces, and thousands separators.
    # European grouping (1.234,56) is intentionally not supported.
    for ch in ("$", "€", "£", "₹", "৳", "USD", "usd", "BDT", "bdt", " "):
        s = s.replace(ch, "")
    s = s.replace(",", "")  # 6,000.00 → 6000.00
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")

def current_user():
    """Cached per request — this runs on every response via inject_globals."""
    if "_current_user" in g:
        return g._current_user
    uid = session.get("user_id")
    user = q1("SELECT id, name, email, role FROM users WHERE id=%s", (uid,)) if uid else None
    g._current_user = user
    return user


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in.", "warning")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user or user.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def log_activity(type_, description, presale_id=None, sold_id=None):
    try:
        run(
            "INSERT INTO activities (type, description, presale_id, sold_id) VALUES (%s,%s,%s,%s)",
            (type_, description, presale_id, sold_id),
        )
    except Exception:
        # Never break the user's action because the audit trail failed,
        # but do surface it — this silently swallowed every error before.
        log.warning("log_activity failed (%s): %s", type_, description, exc_info=True)


def get_stages():
    if "_stages" in g:
        return g._stages
    g._stages = q("SELECT * FROM pipeline_stages ORDER BY sort_order ASC, id ASC")
    return g._stages


# The canonical workflow. Imported data predates it and carries other values,
# so get_statuses() unions these with whatever is actually stored — otherwise a
# row whose status is not in the list renders as the first <option> and appears
# to have a status it does not have.
STATUS_CHOICES = [
    "New", "Brief Submitted", "Replied", "Proposal Sent", "Sold", "Passed",
]


def get_statuses():
    if "_statuses" in g:
        return g._statuses
    extra = [
        r["status"]
        for r in q(
            "SELECT DISTINCT status FROM presales "
            "WHERE status IS NOT NULL AND status <> '' "
            "AND status <> ALL(%s) ORDER BY status",
            (STATUS_CHOICES,),
        )
    ]
    g._statuses = STATUS_CHOICES + extra
    return g._statuses


def get_custom_fields(entity):
    return q(
        "SELECT * FROM custom_fields WHERE entity=%s ORDER BY sort_order ASC, id ASC",
        (entity,),
    )


def collect_custom_data(entity, form):
    fields = get_custom_fields(entity)
    data = {}
    for f in fields:
        key = f["field_key"]
        raw = form.get(f"cf_{key}", "").strip()
        if f["field_type"] == "number":
            try:
                data[key] = float(raw) if raw else None
            except ValueError:
                data[key] = None
        else:
            data[key] = raw or None
    return data


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "today": date.today().isoformat(),
        "stages": get_stages() if session.get("user_id") else [],
        "statuses": get_statuses() if session.get("user_id") else STATUS_CHOICES,
        "commission_rate": COMMISSION_RATE,
        "static_url": static_url,
    }


_static_versions = {}


def static_url(filename):
    """/static/<file>?v=<mtime> — lets the year-long cache header be safe.

    The stamp is cached per process in production; in development it is
    recomputed so edits show up on reload without a hard refresh.
    """
    if IS_PROD and filename in _static_versions:
        stamp = _static_versions[filename]
    else:
        try:
            stamp = str(int(os.stat(os.path.join(app.static_folder, filename)).st_mtime))
        except OSError:
            stamp = "0"
        _static_versions[filename] = stamp
    return url_for("static", filename=filename, v=stamp)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if IS_PROD:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp


@app.template_filter("money")
def money_filter(value):
    """$1,234 — one place to change currency formatting."""
    try:
        return "${:,.0f}".format(float(value or 0))
    except (TypeError, ValueError):
        return "$0"


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
@app.errorhandler(Exception)
def internal_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    # Log the traceback server-side; never render it to the browser.
    log.exception("Unhandled error on %s %s", request.method, request.path)
    return render_template("500.html"), 500


@app.errorhandler(413)
def too_large(e):
    flash("That file is too large (5 MB maximum).", "danger")
    return redirect(url_for("import_data")), 302


@app.route("/health")
def health():
    """Platform health check — verifies the DB round-trips, not just that Flask is up."""
    try:
        scalar("SELECT 1")
        return jsonify({"status": "ok", "database": "up"}), 200
    except Exception as exc:
        log.error("Health check failed: %s", exc)
        return jsonify({"status": "degraded", "database": "down"}), 503


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


def _safe_next(target):
    """Only allow same-site relative redirects, never absolute URLs."""
    if not target:
        return None
    if target.startswith("/") and not target.startswith("//"):
        return target
    return None


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 50 per hour", methods=["POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = q1("SELECT * FROM users WHERE email=%s", (email,))
        if user and verify_password(password, user["password_hash"]):
            # Transparently upgrade legacy sha256 hashes on successful login.
            if _is_legacy_hash(user["password_hash"]):
                try:
                    run(
                        "UPDATE users SET password_hash=%s WHERE id=%s",
                        (hash_password(password), user["id"]),
                    )
                    log.info("Upgraded password hash for user %s", user["id"])
                except Exception:
                    log.warning("Hash upgrade failed for user %s", user["id"], exc_info=True)

            session.permanent = True
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["user_role"] = user["role"]
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(_safe_next(request.args.get("next")) or url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def register():
    """First user (no users yet) OR valid invite token."""
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    user_count = int(scalar("SELECT COUNT(*) FROM users") or 0)
    token = request.args.get("token") or request.form.get("token", "")
    invite = None
    if token:
        invite = q1(
            "SELECT * FROM invitations WHERE token=%s AND used=FALSE AND (expires_at IS NULL OR expires_at > NOW())",
            (token,),
        )

    first_user = user_count == 0
    if not first_user and not invite:
        flash("Registration is invite-only. Ask an admin for an invitation link.", "warning")
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("register.html", first_user=first_user, token=token, invite=invite)
        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html", first_user=first_user, token=token, invite=invite)
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("register.html", first_user=first_user, token=token, invite=invite)
        if q1("SELECT id FROM users WHERE email=%s", (email,)):
            flash("Email already registered.", "danger")
            return render_template("register.html", first_user=first_user, token=token, invite=invite)

        if first_user:
            role = "admin"
        else:
            if invite and invite["email"].lower() != email:
                flash("Invite email does not match.", "danger")
                return render_template("register.html", first_user=False, token=token, invite=invite)
            role = invite["role"] if invite else "member"

        user = insert_returning(
            "INSERT INTO users (name, email, password_hash, role) VALUES (%s,%s,%s,%s) RETURNING id, name, role",
            (name, email, hash_password(password), role),
        )
        if invite:
            run("UPDATE invitations SET used=TRUE WHERE id=%s", (invite["id"],))

        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_role"] = user["role"]
        flash(f"Account created! Welcome, {name}.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html", first_user=first_user, token=token, invite=invite)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("index"))


# ── Team & Invites (admin) ────────────────────────────────────────────────────

@app.route("/team")
@login_required
@admin_required
def team():
    members = q("SELECT id, name, email, role, created_at FROM users ORDER BY created_at")
    invites = q(
        "SELECT * FROM invitations WHERE used=FALSE ORDER BY created_at DESC"
    )
    return render_template("team.html", members=members, invites=invites)


@app.route("/team/invite", methods=["POST"])
@login_required
@admin_required
def team_invite():
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "member")
    if role not in ("admin", "member"):
        role = "member"
    if not email:
        flash("Email required.", "danger")
        return redirect(url_for("team"))
    if q1("SELECT id FROM users WHERE email=%s", (email,)):
        flash("User already exists.", "danger")
        return redirect(url_for("team"))
    token = secrets.token_urlsafe(32)
    expires = date.today() + timedelta(days=7)
    run(
        "INSERT INTO invitations (email, token, role, invited_by, expires_at) VALUES (%s,%s,%s,%s,%s)",
        (email, token, role, session["user_id"], expires),
    )
    invite_url = url_for("register", token=token, _external=True)
    flash(f"Invite created for {email}. Link: {invite_url}", "success")
    return redirect(url_for("team"))


@app.route("/team/<int:uid>/role", methods=["POST"])
@login_required
@admin_required
def team_role(uid):
    role = request.form.get("role", "member")
    if role not in ("admin", "member"):
        role = "member"
    if uid == session.get("user_id") and role != "admin":
        flash("You cannot demote yourself.", "danger")
        return redirect(url_for("team"))
    run("UPDATE users SET role=%s WHERE id=%s", (role, uid))
    flash("Role updated.", "success")
    return redirect(url_for("team"))


@app.route("/team/<int:uid>/delete", methods=["POST"])
@login_required
@admin_required
def team_delete(uid):
    if uid == session.get("user_id"):
        flash("Cannot delete yourself.", "danger")
        return redirect(url_for("team"))
    run("DELETE FROM users WHERE id=%s", (uid,))
    flash("Member removed.", "info")
    return redirect(url_for("team"))


@app.route("/team/invite/<int:iid>/revoke", methods=["POST"])
@login_required
@admin_required
def invite_revoke(iid):
    run("DELETE FROM invitations WHERE id=%s AND used=FALSE", (iid,))
    flash("Invite revoked.", "info")
    return redirect(url_for("team"))


# ── Settings: stages + custom fields ──────────────────────────────────────────

@app.route("/settings")
@login_required
@admin_required
def settings():
    return render_template(
        "settings.html",
        stages=get_stages(),
        presale_fields=get_custom_fields("presale"),
        sold_fields=get_custom_fields("sold"),
    )


@app.route("/settings/stages", methods=["POST"])
@login_required
@admin_required
def settings_stages():
    action = request.form.get("action")
    if action == "add":
        key = request.form.get("key", "").strip().lower().replace(" ", "_")
        label = request.form.get("label", "").strip()
        color = request.form.get("color", "#6b7280")
        if key and label:
            max_ord = scalar("SELECT COALESCE(MAX(sort_order),0) FROM pipeline_stages") or 0
            try:
                run(
                    "INSERT INTO pipeline_stages (key, label, sort_order, color) VALUES (%s,%s,%s,%s)",
                    (key, label, int(max_ord) + 1, color),
                )
                flash("Stage added.", "success")
            except Exception as e:
                flash(f"Could not add stage: {e}", "danger")
    elif action == "rename":
        sid = request.form.get("id")
        label = request.form.get("label", "").strip()
        color = request.form.get("color", "#6b7280")
        run("UPDATE pipeline_stages SET label=%s, color=%s WHERE id=%s", (label, color, sid))
        flash("Stage updated.", "success")
    elif action == "delete":
        sid = request.form.get("id")
        stage = q1("SELECT key FROM pipeline_stages WHERE id=%s", (sid,))
        if stage:
            run("DELETE FROM pipeline_stages WHERE id=%s", (sid,))
            flash("Stage deleted.", "info")
    elif action == "reorder":
        # ids in order: "3,1,2"
        order = request.form.get("order", "")
        for i, sid in enumerate(order.split(",")):
            if sid.strip().isdigit():
                run("UPDATE pipeline_stages SET sort_order=%s WHERE id=%s", (i + 1, int(sid)))
        flash("Order saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/fields", methods=["POST"])
@login_required
@admin_required
def settings_fields():
    action = request.form.get("action")
    entity = request.form.get("entity", "presale")
    if action == "add":
        key = request.form.get("field_key", "").strip().lower().replace(" ", "_")
        label = request.form.get("label", "").strip()
        ftype = request.form.get("field_type", "text")
        options_raw = request.form.get("options", "").strip()
        options = None
        if ftype == "select" and options_raw:
            options = json.dumps([o.strip() for o in options_raw.split(",") if o.strip()])
        if key and label:
            max_ord = scalar(
                "SELECT COALESCE(MAX(sort_order),0) FROM custom_fields WHERE entity=%s", (entity,)
            ) or 0
            try:
                run(
                    """INSERT INTO custom_fields (entity, field_key, label, field_type, options, sort_order)
                       VALUES (%s,%s,%s,%s,%s::jsonb,%s)""",
                    (entity, key, label, ftype, options, int(max_ord) + 1),
                )
                flash("Field added.", "success")
            except Exception as e:
                flash(f"Could not add field: {e}", "danger")
    elif action == "delete":
        fid = request.form.get("id")
        run("DELETE FROM custom_fields WHERE id=%s", (fid,))
        flash("Field removed.", "info")
    return redirect(url_for("settings"))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    total_presales = int(scalar("SELECT COUNT(*) FROM presales") or 0)
    total_quoted = float(
        scalar("SELECT COALESCE(SUM(quoted_amount),0) FROM presales WHERE status != 'Passed'") or 0
    )
    total_sold = int(scalar("SELECT COUNT(*) FROM sold") or 0)
    total_revenue = float(
        scalar("SELECT COALESCE(SUM(order_amount + bonus_amount),0) FROM sold") or 0
    )
    won_net = total_revenue * COMMISSION_RATE
    open_wip = int(scalar("SELECT COUNT(*) FROM sold WHERE status IN ('WIP','Revision')") or 0)
    overdue = int(
        scalar(
            """SELECT COUNT(*) FROM sold
               WHERE status IN ('WIP','Revision')
                 AND deli_last_date IS NOT NULL AND deli_last_date < CURRENT_DATE"""
        ) or 0
    )
    status_breakdown = q(
        "SELECT status, COUNT(*) AS cnt, COALESCE(SUM(quoted_amount),0) AS val FROM presales GROUP BY status"
    )
    try:
        recent = q(
            """SELECT a.*, p.client_username, s.client_name, s.project_name
               FROM activities a
               LEFT JOIN presales p ON a.presale_id = p.id
               LEFT JOIN sold s ON a.sold_id = s.id
               ORDER BY a.created_at DESC LIMIT 12"""
        )
    except Exception:
        recent = []

    stage_stats = q(
        """SELECT COALESCE(stage_key, 'lead') AS stage_key, COUNT(*) AS cnt,
                  COALESCE(SUM(quoted_amount),0) AS val
           FROM presales GROUP BY stage_key"""
    )

    return render_template(
        "dashboard.html",
        total_presales=total_presales,
        total_quoted=total_quoted,
        total_sold=total_sold,
        total_revenue=total_revenue,
        won_net=won_net,
        open_wip=open_wip,
        overdue=overdue,
        status_breakdown=status_breakdown,
        stage_stats=stage_stats,
        recent=recent,
    )


# ── Reports ───────────────────────────────────────────────────────────────────

STATUS_COLORS = {
    "New": "secondary",
    "Brief Submitted": "info",
    "Replied": "warning",
    "Proposal Sent": "primary",
    "Sold": "success",
    "Passed": "secondary",
}


@app.route("/reports")
@login_required
def reports():
    """Conversion, category, and monthly performance — wired to reports.html."""
    today = date.today()
    month_start = today.replace(day=1)
    month_end = today + timedelta(days=1)

    total_leads = int(scalar("SELECT COUNT(*) FROM presales") or 0)
    briefs_submitted = int(
        scalar("SELECT COUNT(*) FROM presales WHERE status='Brief Submitted'") or 0
    )
    proposals_sent = int(
        scalar("SELECT COUNT(*) FROM presales WHERE status='Proposal Sent'") or 0
    )
    sold_count = int(scalar("SELECT COUNT(*) FROM sold") or 0)
    brief_rate = (briefs_submitted / total_leads * 100) if total_leads else 0.0
    conversion_rate = (sold_count / total_leads * 100) if total_leads else 0.0

    total_revenue = float(
        scalar(
            "SELECT COALESCE(SUM(order_amount + bonus_amount),0) * %s FROM sold",
            (COMMISSION_RATE,),
        )
        or 0
    )
    total_quoted = float(
        scalar(
            "SELECT COALESCE(SUM(quoted_amount),0) FROM presales WHERE status != 'Passed'"
        )
        or 0
    )

    # A presale counts as "sold" here only when it has been converted into a
    # sold order (presale_id link). Quoted sums feed the per-category table.
    rows = q(
        """SELECT COALESCE(NULLIF(category,''),'Other') AS category,
                  COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN id IN
                      (SELECT presale_id FROM sold WHERE presale_id IS NOT NULL)
                      THEN 1 ELSE 0 END),0) AS sold,
                  COALESCE(SUM(quoted_amount),0) AS quoted
           FROM presales GROUP BY 1 ORDER BY quoted DESC"""
    )
    by_category = {r["category"]: r for r in rows}

    monthly_leads = q(
        """SELECT * FROM presales
           WHERE date >= %s AND date < %s
           ORDER BY date DESC, id DESC""",
        (month_start, month_end),
    )

    return render_template(
        "reports.html",
        total_leads=total_leads,
        briefs_submitted=briefs_submitted,
        brief_rate=brief_rate,
        proposals_sent=proposals_sent,
        sold_count=sold_count,
        conversion_rate=conversion_rate,
        total_revenue=total_revenue,
        total_quoted=total_quoted,
        by_category=by_category,
        monthly_leads=monthly_leads,
        status_colors=STATUS_COLORS,
    )


# ── Presales ──────────────────────────────────────────────────────────────────

@app.route("/leads")
@login_required
def leads():
    search = request.args.get("search", "").strip()
    status_f = request.args.get("status", "")
    shift_f = request.args.get("shift", "")
    cat_f = request.args.get("category", "")
    stage_f = request.args.get("stage", "")
    month_f = request.args.get("month", "")
    view = request.args.get("view", "list")

    sql = "SELECT * FROM presales WHERE 1=1"
    args = []
    if search:
        sql += " AND (client_username ILIKE %s OR profile_name ILIKE %s OR remarks ILIKE %s)"
        args += [f"%{search}%"] * 3
    if status_f:
        sql += " AND status=%s"
        args.append(status_f)
    if shift_f:
        sql += " AND shift=%s"
        args.append(shift_f)
    if cat_f:
        sql += " AND category=%s"
        args.append(cat_f)
    if stage_f:
        sql += " AND stage_key=%s"
        args.append(stage_f)
    if month_f:
        sql += " AND to_char(date, 'YYYY-MM') = %s"
        args.append(month_f)
    sql += " ORDER BY date DESC NULLS LAST, id DESC"

    rows = q(sql, args)
    total_quoted = sum(float(r.get("quoted_amount") or 0) for r in rows)
    custom_fields = get_custom_fields("presale")

    if view == "kanban":
        board = {s["key"]: [] for s in get_stages()}
        board.setdefault("lead", [])
        for r in rows:
            k = r.get("stage_key") or "lead"
            board.setdefault(k, []).append(r)
        return render_template(
            "leads.html",
            leads=rows,
            board=board,
            view="kanban",
            total_leads=len(rows),
            total_quoted=total_quoted,
            search=search,
            status_filter=status_f,
            shift_filter=shift_f,
            category_filter=cat_f,
            stage_filter=stage_f,
            month_filter=month_f,
            custom_fields=custom_fields,
        )

    return render_template(
        "leads.html",
        leads=rows,
        view="list",
        total_leads=len(rows),
        total_quoted=total_quoted,
        search=search,
        status_filter=status_f,
        shift_filter=shift_f,
        category_filter=cat_f,
        stage_filter=stage_f,
        month_filter=month_f,
        custom_fields=custom_fields,
    )


@app.route("/leads/new", methods=["GET", "POST"])
@login_required
def leads_new():
    custom_fields = get_custom_fields("presale")
    if request.method == "POST":
        client = request.form.get("client_username", "").strip()
        if not client:
            flash("Client Username is required.", "danger")
            return render_template("lead_form.html", lead=None, custom_fields=custom_fields)
        custom = collect_custom_data("presale", request.form)
        run(
            """INSERT INTO presales
               (date, shift, source, url, client_username, profile_name, category,
                quoted_amount, status, stage_key, first_followup, second_followup,
                checked_by, screenshot_reason, remarks, custom_data)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                parse_date(request.form.get("date")) or date.today(),
                request.form.get("shift") or None,
                request.form.get("source") or None,
                request.form.get("url") or None,
                client,
                request.form.get("profile_name") or None,
                request.form.get("category") or None,
                parse_money(request.form.get("quoted_amount")),
                request.form.get("status") or "New",
                request.form.get("stage_key") or "lead",
                bool(request.form.get("first_followup")),
                bool(request.form.get("second_followup")),
                request.form.get("checked_by") or None,
                request.form.get("screenshot_reason") or None,
                request.form.get("remarks") or None,
                json.dumps(custom),
            ),
        )
        log_activity("presale_created", f"Presale lead '{client}' added")
        flash(f"Lead '{client}' created.", "success")
        return redirect(url_for("leads"))
    return render_template("lead_form.html", lead=None, custom_fields=custom_fields)


@app.route("/leads/<int:id>/edit", methods=["GET", "POST"])
@login_required
def leads_edit(id):
    lead = q1("SELECT * FROM presales WHERE id=%s", (id,))
    if not lead:
        flash("Lead not found.", "danger")
        return redirect(url_for("leads"))
    custom_fields = get_custom_fields("presale")
    if request.method == "POST":
        client = request.form.get("client_username", "").strip()
        custom = collect_custom_data("presale", request.form)
        run(
            """UPDATE presales SET
               date=%s, shift=%s, source=%s, url=%s, client_username=%s, profile_name=%s,
               category=%s, quoted_amount=%s, status=%s, stage_key=%s,
               first_followup=%s, second_followup=%s, checked_by=%s, screenshot_reason=%s,
               remarks=%s, custom_data=%s::jsonb, updated_at=NOW()
               WHERE id=%s""",
            (
                parse_date(request.form.get("date")),
                request.form.get("shift") or None,
                request.form.get("source") or None,
                request.form.get("url") or None,
                client,
                request.form.get("profile_name") or None,
                request.form.get("category") or None,
                parse_money(request.form.get("quoted_amount")),
                request.form.get("status") or "New",
                request.form.get("stage_key") or "lead",
                bool(request.form.get("first_followup")),
                bool(request.form.get("second_followup")),
                request.form.get("checked_by") or None,
                request.form.get("screenshot_reason") or None,
                request.form.get("remarks") or None,
                json.dumps(custom),
                id,
            ),
        )
        log_activity("presale_updated", f"Presale lead '{client}' updated", presale_id=id)
        flash("Lead updated.", "success")
        return redirect(url_for("leads"))
    # normalize custom_data
    if isinstance(lead.get("custom_data"), str):
        lead["custom_data"] = json.loads(lead["custom_data"] or "{}")
    elif lead.get("custom_data") is None:
        lead["custom_data"] = {}
    return render_template("lead_form.html", lead=lead, custom_fields=custom_fields)


@app.route("/leads/<int:id>/delete", methods=["POST"])
@login_required
def leads_delete(id):
    run("DELETE FROM presales WHERE id=%s", (id,))
    flash("Lead deleted.", "info")
    return redirect(url_for("leads"))


@app.route("/leads/<int:id>/status", methods=["POST"])
@login_required
def leads_status(id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if not status:
        return jsonify({"error": "Missing status"}), 400
    if status not in get_statuses():
        return jsonify({"error": "Invalid status"}), 400
    run("UPDATE presales SET status=%s, updated_at=NOW() WHERE id=%s", (status, id))
    log_activity("presale_status", f"Status → {status}", presale_id=id)
    return jsonify({"ok": True})


@app.route("/leads/<int:id>/stage", methods=["POST"])
@login_required
def leads_stage(id):
    data = request.get_json(silent=True) or {}
    stage = data.get("stage")
    keys = {s["key"] for s in get_stages()}
    if stage not in keys:
        return jsonify({"error": "Invalid stage"}), 400
    run("UPDATE presales SET stage_key=%s, updated_at=NOW() WHERE id=%s", (stage, id))
    log_activity("stage_changed", f"Stage → {stage}", presale_id=id)
    return jsonify({"ok": True})


@app.route("/leads/<int:id>/mark-sold", methods=["GET", "POST"])
@login_required
def leads_mark_sold(id):
    lead = q1("SELECT * FROM presales WHERE id=%s", (id,))
    if not lead:
        flash("Lead not found.", "danger")
        return redirect(url_for("leads"))
    custom_fields = get_custom_fields("sold")
    if request.method == "POST":
        client_name = request.form.get("client_name", "").strip() or lead.get("client_username") or "Unknown"
        custom = collect_custom_data("sold", request.form)
        run(
            """INSERT INTO sold
               (date, account, service_type, order_id, project_name, client_name,
                status, assign_leader, developer, deli_last_date,
                order_amount, bonus_amount, sheet_link, comment, presale_id, custom_data)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                parse_date(request.form.get("date")) or date.today(),
                request.form.get("account") or lead.get("profile_name"),
                request.form.get("service_type") or lead.get("category"),
                request.form.get("order_id") or None,
                request.form.get("project_name") or None,
                client_name,
                request.form.get("status") or "WIP",
                request.form.get("assign_leader") or None,
                request.form.get("developer") or None,
                parse_date(request.form.get("deli_last_date")),
                parse_money(request.form.get("order_amount") or lead.get("quoted_amount")),
                parse_money(request.form.get("bonus_amount")),
                request.form.get("sheet_link") or None,
                request.form.get("comment") or lead.get("remarks"),
                id,
                json.dumps(custom),
            ),
        )
        run("UPDATE presales SET status=%s, updated_at=NOW() WHERE id=%s", ("Sold", id))
        log_activity("mark_sold", f"Lead marked Sold → {client_name}", presale_id=id)
        flash(f"Marked as Sold: {client_name}", "success")
        return redirect(url_for("clients"))

    prefill = {
        "date": date.today(),
        "account": lead.get("profile_name"),
        "service_type": lead.get("category"),
        "client_name": lead.get("client_username"),
        "order_amount": lead.get("quoted_amount") or 0,
        "comment": lead.get("remarks"),
        "status": "WIP",
        "custom_data": {},
    }
    return render_template(
        "client_form.html", client=prefill, custom_fields=custom_fields,
        from_presale=True, presale_id=id,
    )


# ── Sold / Orders ─────────────────────────────────────────────────────────────

@app.route("/clients")
@login_required
def clients():
    search = request.args.get("search", "").strip()
    status_f = request.args.get("status", "")
    month_f = request.args.get("month", "")
    sql = "SELECT * FROM sold WHERE 1=1"
    args = []
    if search:
        sql += " AND (client_name ILIKE %s OR project_name ILIKE %s OR account ILIKE %s OR developer ILIKE %s)"
        args += [f"%{search}%"] * 4
    if status_f:
        sql += " AND status=%s"
        args.append(status_f)
    if month_f:
        sql += " AND to_char(date, 'YYYY-MM') = %s"
        args.append(month_f)
    sql += " ORDER BY date DESC NULLS LAST, id DESC"
    rows = q(sql, args)
    total_orders = sum(float(r.get("order_amount") or 0) + float(r.get("bonus_amount") or 0) for r in rows)
    return render_template(
        "clients.html",
        clients=rows,
        total_orders=total_orders,
        total_revenue=total_orders * COMMISSION_RATE,
        search=search,
        status_filter=status_f,
        month_filter=month_f,
    )


@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def clients_new():
    custom_fields = get_custom_fields("sold")
    if request.method == "POST":
        client_name = request.form.get("client_name", "").strip()
        if not client_name:
            flash("Client Name is required.", "danger")
            return render_template("client_form.html", client=None, custom_fields=custom_fields)
        custom = collect_custom_data("sold", request.form)
        run(
            """INSERT INTO sold
               (date, account, service_type, order_id, project_name, client_name,
                status, assign_leader, developer, deli_last_date,
                order_amount, bonus_amount, sheet_link, comment, custom_data)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                parse_date(request.form.get("date")) or date.today(),
                request.form.get("account") or None,
                request.form.get("service_type") or None,
                request.form.get("order_id") or None,
                request.form.get("project_name") or None,
                client_name,
                request.form.get("status") or "WIP",
                request.form.get("assign_leader") or None,
                request.form.get("developer") or None,
                parse_date(request.form.get("deli_last_date")),
                parse_money(request.form.get("order_amount")),
                parse_money(request.form.get("bonus_amount")),
                request.form.get("sheet_link") or None,
                request.form.get("comment") or None,
                json.dumps(custom),
            ),
        )
        log_activity("sold_created", f"Order for '{client_name}' created")
        flash(f"Order for '{client_name}' created.", "success")
        return redirect(url_for("clients"))
    return render_template("client_form.html", client=None, custom_fields=custom_fields)


@app.route("/clients/<int:id>/edit", methods=["GET", "POST"])
@login_required
def clients_edit(id):
    client = q1("SELECT * FROM sold WHERE id=%s", (id,))
    if not client:
        flash("Order not found.", "danger")
        return redirect(url_for("clients"))
    custom_fields = get_custom_fields("sold")
    if request.method == "POST":
        client_name = request.form.get("client_name", "").strip()
        custom = collect_custom_data("sold", request.form)
        run(
            """UPDATE sold SET
               date=%s, account=%s, service_type=%s, order_id=%s, project_name=%s, client_name=%s,
               status=%s, assign_leader=%s, developer=%s, deli_last_date=%s,
               order_amount=%s, bonus_amount=%s, sheet_link=%s, comment=%s,
               custom_data=%s::jsonb, updated_at=NOW()
               WHERE id=%s""",
            (
                parse_date(request.form.get("date")),
                request.form.get("account") or None,
                request.form.get("service_type") or None,
                request.form.get("order_id") or None,
                request.form.get("project_name") or None,
                client_name,
                request.form.get("status") or "WIP",
                request.form.get("assign_leader") or None,
                request.form.get("developer") or None,
                parse_date(request.form.get("deli_last_date")),
                parse_money(request.form.get("order_amount")),
                parse_money(request.form.get("bonus_amount")),
                request.form.get("sheet_link") or None,
                request.form.get("comment") or None,
                json.dumps(custom),
                id,
            ),
        )
        log_activity("sold_updated", f"Order '{client_name}' updated", sold_id=id)
        flash("Order updated.", "success")
        return redirect(url_for("clients"))
    if isinstance(client.get("custom_data"), str):
        client["custom_data"] = json.loads(client["custom_data"] or "{}")
    elif client.get("custom_data") is None:
        client["custom_data"] = {}
    return render_template("client_form.html", client=client, custom_fields=custom_fields)


@app.route("/clients/<int:id>/delete", methods=["POST"])
@login_required
def clients_delete(id):
    run("DELETE FROM sold WHERE id=%s", (id,))
    flash("Order deleted.", "info")
    return redirect(url_for("clients"))


# ── CSV Import / Export ───────────────────────────────────────────────────────

@app.route("/import", methods=["GET", "POST"])
@login_required
def import_data():
    if request.method == "POST":
        file = request.files.get("csv_file")
        kind = request.form.get("kind", "presales")
        if not file:
            flash("No file selected.", "danger")
            return render_template("import.html")
        try:
            content = file.stream.read().decode("utf-8-sig")
        except Exception:
            try:
                file.stream.seek(0)
            except Exception:
                pass
            content = file.stream.read().decode("latin-1")
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            flash("Could not read CSV headers.", "danger")
            return render_template("import.html")

        def norm(h):
            return (h or "").strip().lower().replace(" ", "_").replace("-", "_")

        count = 0
        errors = 0
        if kind == "presales":
            for row in reader:
                try:
                    r = {norm(k): (v or "").strip() for k, v in row.items()}
                    client = (
                        r.get("client_username")
                        or r.get("client")
                        or r.get("client_name")
                        or r.get("username")
                        or ""
                    )
                    if not client:
                        continue
                    run(
                        """INSERT INTO presales
                           (date, shift, source, url, client_username, profile_name,
                            category, quoted_amount, status, remarks)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            parse_date(r.get("date")),
                            r.get("shift") or None,
                            r.get("source") or None,
                            r.get("url") or None,
                            client,
                            r.get("profile_name") or r.get("profile") or None,
                            r.get("category") or None,
                            parse_money(
                                r.get("quoted_amount")
                                or r.get("amount")
                                or r.get("quoted")
                                or 0
                            ),
                            r.get("status") or "New",
                            r.get("remarks") or r.get("comment") or r.get("notes") or None,
                        ),
                    )
                    count += 1
                except Exception:
                    errors += 1
                    continue
            msg = f"Imported {count} Presales records."
            if errors:
                msg += f" Skipped {errors} rows with errors."
            flash(msg, "success" if count else "warning")
            return redirect(url_for("leads"))
        else:
            for row in reader:
                try:
                    r = {norm(k): (v or "").strip() for k, v in row.items()}
                    client = (
                        r.get("client_name")
                        or r.get("client")
                        or r.get("client_username")
                        or ""
                    )
                    if not client:
                        continue
                    run(
                        """INSERT INTO sold
                           (date, account, service_type, project_name, client_name,
                            status, assign_leader, developer, deli_last_date,
                            order_amount, sheet_link, comment)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            parse_date(r.get("date")),
                            r.get("account") or None,
                            r.get("service_type") or r.get("service") or r.get("category") or None,
                            r.get("project_name") or r.get("project") or None,
                            client,
                            r.get("status") or "WIP",
                            r.get("assign")
                            or r.get("assign_leader")
                            or r.get("leader")
                            or None,
                            r.get("developer") or None,
                            parse_date(
                                r.get("deli_last_date")
                                or r.get("delivery_date")
                                or r.get("deadline")
                            ),
                            parse_money(
                                r.get("order_amount")
                                or r.get("amount")
                                or r.get("order")
                                or 0
                            ),
                            r.get("sheet_link") or r.get("sheet") or r.get("link") or None,
                            r.get("comment") or r.get("remarks") or r.get("notes") or None,
                        ),
                    )
                    count += 1
                except Exception:
                    errors += 1
                    continue
            msg = f"Imported {count} Sold records."
            if errors:
                msg += f" Skipped {errors} rows with errors."
            flash(msg, "success" if count else "warning")
            return redirect(url_for("clients"))
    return render_template("import.html")

@app.route("/export/leads")
@login_required
def export_leads():
    rows = q(
        """SELECT date, shift, source, url, client_username, profile_name,
                  category, quoted_amount, status, stage_key, remarks, created_at
           FROM presales ORDER BY date DESC NULLS LAST"""
    )
    fields = [
        "date", "shift", "source", "url", "client_username", "profile_name",
        "category", "quoted_amount", "status", "stage_key", "remarks", "created_at",
    ]
    def gen():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})
        yield buf.getvalue()
    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=presales.csv"})


@app.route("/export/clients")
@login_required
def export_clients():
    rows = q(
        """SELECT date, account, service_type, project_name, client_name, status,
                  assign_leader, developer, deli_last_date, order_amount,
                  bonus_amount, sheet_link, comment, created_at
           FROM sold ORDER BY date DESC NULLS LAST"""
    )
    fields = [
        "date", "account", "service_type", "project_name", "client_name", "status",
        "assign_leader", "developer", "deli_last_date", "order_amount",
        "bonus_amount", "sheet_link", "comment", "created_at",
    ]
    def gen():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})
        yield buf.getvalue()
    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=sold_orders.csv"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
