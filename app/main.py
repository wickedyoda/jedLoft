import logging
import os
import re
import zipfile
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import hash_password, verify_password
from .database import Base, SessionLocal, engine
from .models import Bird, FlightLog, PasswordHistory, User

load_dotenv()

ROLE_ADMIN = "admin"
ROLE_READ_ONLY = "read_only"
PASSWORD_MIN_LENGTH = 6
PASSWORD_MIN_UPPERCASE = 2
VALID_THEMES = {"standard", "colorblind"}
VALID_TEXT_SIZES = {"small", "medium", "large"}

LOG_DIR = Path(os.getenv("LOG_DIR", "/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("jedloft")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    log_handler = RotatingFileHandler(LOG_DIR / "jedloft.log", maxBytes=3_000_000, backupCount=10)
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(log_handler)

app = FastAPI(title="jedLoft Web")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-secret-change-me"),
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def log_event(event: str, **fields: object) -> None:
    details = " ".join(
        f"{key}={str(value).replace(' ', '_')}" for key, value in sorted(fields.items()) if value is not None
    )
    logger.info("event=%s %s", event, details)


def ensure_user_table_columns() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(255)"))
        conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(32) DEFAULT 'read_only' NOT NULL")
        )
        conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN DEFAULT FALSE NOT NULL")
        )
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_enabled BOOLEAN DEFAULT TRUE NOT NULL"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(32) DEFAULT 'standard' NOT NULL"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS text_size VARCHAR(16) DEFAULT 'medium' NOT NULL"))
        conn.execute(text("UPDATE users SET name = split_part(email, '@', 1) WHERE name IS NULL"))
        conn.execute(text("UPDATE users SET role = 'read_only' WHERE role IS NULL"))
        conn.execute(text("UPDATE users SET is_approved = TRUE WHERE is_approved IS NULL"))
        conn.execute(text("UPDATE users SET is_enabled = TRUE WHERE is_enabled IS NULL"))
        conn.execute(text("UPDATE users SET theme = 'standard' WHERE theme IS NULL"))
        conn.execute(text("UPDATE users SET text_size = 'medium' WHERE text_size IS NULL"))


def ensure_bird_table_columns() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE birds ADD COLUMN IF NOT EXISTS racing_homer_notes VARCHAR(1500)"))


def validate_password_policy(password: str) -> str | None:
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    uppercase_count = len(re.findall(r"[A-Z]", password))
    if uppercase_count < PASSWORD_MIN_UPPERCASE:
        return f"Password must include at least {PASSWORD_MIN_UPPERCASE} uppercase letters."
    return None


def get_recent_password_hashes(user: User, db: Session, limit: int = 2) -> list[str]:
    rows = (
        db.query(PasswordHistory)
        .filter(PasswordHistory.user_id == user.id)
        .order_by(PasswordHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [row.password_hash for row in rows]


def is_password_reused(user: User, new_password: str, db: Session) -> bool:
    if verify_password(new_password, user.password_hash):
        return True
    return any(verify_password(new_password, old_hash) for old_hash in get_recent_password_hashes(user, db, limit=2))


def set_user_password(user: User, new_password: str, db: Session) -> None:
    if user.password_hash:
        db.add(PasswordHistory(user_id=user.id, password_hash=user.password_hash))
    user.password_hash = hash_password(new_password)

    keep_ids = [
        row.id
        for row in (
            db.query(PasswordHistory)
            .filter(PasswordHistory.user_id == user.id)
            .order_by(PasswordHistory.created_at.desc(), PasswordHistory.id.desc())
            .limit(2)
            .all()
        )
    ]
    if keep_ids:
        (
            db.query(PasswordHistory)
            .filter(PasswordHistory.user_id == user.id, PasswordHistory.id.notin_(keep_ids))
            .delete(synchronize_session=False)
        )


def bootstrap_default_admin() -> None:
    admin_name = os.getenv("DEFAULT_ADMIN_NAME", "").strip()
    admin_email = os.getenv("DEFAULT_ADMIN_EMAIL", "").lower().strip()
    admin_password = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
    if not admin_name or not admin_email or not admin_password:
        return

    password_error = validate_password_policy(admin_password)
    if password_error:
        logger.warning("Default admin password is invalid for configured policy.")
        return

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == admin_email).first()
        if existing:
            return
        admin = User(
            name=admin_name,
            email=admin_email,
            role=ROLE_ADMIN,
            is_approved=True,
            is_enabled=True,
            theme="standard",
            text_size="medium",
            password_hash=hash_password(admin_password),
        )
        db.add(admin)
        db.commit()
        log_event("bootstrap_admin_created", email=admin_email)
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_user_table_columns()
    ensure_bird_table_columns()
    bootstrap_default_admin()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ui_classes(user: User | None) -> tuple[str, str]:
    theme = user.theme if user and user.theme in VALID_THEMES else "standard"
    text_size = user.text_size if user and user.text_size in VALID_TEXT_SIZES else "medium"
    return f"theme-{theme}", f"text-{text_size}"


def render(
    template_name: str,
    request: Request,
    user: User | None = None,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    theme_class, text_size_class = ui_classes(user)
    payload = {
        "request": request,
        "nav_user": user,
        "theme_class": theme_class,
        "text_size_class": text_size_class,
    }
    payload.update(context)
    return templates.TemplateResponse(template_name, payload, status_code=status_code)


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def active_session_user(request: Request, db: Session) -> tuple[User | None, str | None]:
    user = current_user(request, db)
    if not user:
        return None, "missing"
    if not user.is_enabled:
        request.session.clear()
        return None, "disabled"
    if not user.is_approved:
        request.session.clear()
        return None, "pending"
    return user, None


def require_admin(request: Request, db: Session) -> User | None:
    user, _ = active_session_user(request, db)
    if not user or user.role != ROLE_ADMIN:
        return None
    return user


def dashboard_context(user: User, db: Session, error: str | None = None, success: str | None = None) -> dict:
    birds = db.query(Bird).order_by(Bird.created_at.desc(), Bird.id.desc()).all()
    flight_logs = db.query(FlightLog).order_by(FlightLog.flight_date.desc(), FlightLog.id.desc()).all()
    return {
        "user": user,
        "birds": birds,
        "flight_logs": flight_logs,
        "error": error,
        "success": success,
    }


def admin_context(user: User, db: Session, error: str | None = None, success: str | None = None) -> dict:
    pending_users = (
        db.query(User)
        .filter(User.role == ROLE_READ_ONLY, User.is_approved.is_(False))
        .order_by(User.created_at.asc())
        .all()
    )
    all_users = db.query(User).order_by(User.created_at.asc()).all()
    return {
        "user": user,
        "pending_users": pending_users,
        "all_users": all_users,
        "error": error,
        "success": success,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user, _ = active_session_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=302)
    return render("login.html", request, error=None)


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.lower().strip()
    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not verify_password(password, user.password_hash):
        log_event("login_failed", email=normalized_email)
        return render("login.html", request, error="Invalid email or password.", status_code=401)
    if not user.is_enabled:
        log_event("login_blocked_disabled", email=normalized_email)
        return render("login.html", request, error="Your account is disabled.", status_code=403)
    if not user.is_approved:
        log_event("login_blocked_pending", email=normalized_email)
        return render("login.html", request, error="Your account is pending admin approval.", status_code=403)

    request.session["user_id"] = user.id
    log_event("login_success", user_id=user.id, email=user.email)
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=302)
    return render("register.html", request, error=None)


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_name = name.strip()
    normalized_email = email.lower().strip()
    if not normalized_name:
        return render("register.html", request, error="Name is required.", status_code=400)

    password_error = validate_password_policy(password)
    if password_error:
        return render("register.html", request, error=password_error, status_code=400)

    if db.query(User).filter(User.email == normalized_email).first():
        return render("register.html", request, error="Email already registered.", status_code=409)

    user = User(
        name=normalized_name,
        email=normalized_email,
        role=ROLE_READ_ONLY,
        is_approved=False,
        is_enabled=True,
        theme="standard",
        text_size="medium",
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    log_event("register_submitted", user_id=user.id, email=user.email)
    return render(
        "login.html",
        request,
        error="Registration submitted. Wait for admin approval before login.",
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user, state = active_session_user(request, db)
    if not user:
        if state == "disabled":
            return render("login.html", request, error="Your account is disabled.", status_code=403)
        if state == "pending":
            return render("login.html", request, error="Your account is pending admin approval.", status_code=403)
        return RedirectResponse("/login", status_code=302)

    return render("dashboard.html", request, **dashboard_context(user, db))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user, state = active_session_user(request, db)
    if not user:
        if state == "disabled":
            return render("login.html", request, error="Your account is disabled.", status_code=403)
        if state == "pending":
            return render("login.html", request, error="Your account is pending admin approval.", status_code=403)
        return RedirectResponse("/login", status_code=302)

    return render("settings.html", request, user=user, error=None, success=None)


@app.post("/settings/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user, state = active_session_user(request, db)
    if not user:
        if state in {"disabled", "pending"}:
            return render("login.html", request, error="Unable to access account.", status_code=403)
        return RedirectResponse("/login", status_code=302)

    if not verify_password(current_password, user.password_hash):
        return render("settings.html", request, user=user, error="Current password is incorrect.", success=None, status_code=400)

    password_error = validate_password_policy(new_password)
    if password_error:
        return render("settings.html", request, user=user, error=password_error, success=None, status_code=400)

    if is_password_reused(user, new_password, db):
        return render(
            "settings.html",
            request,
            user=user,
            error="Password cannot match your current or last 2 passwords.",
            success=None,
            status_code=400,
        )

    set_user_password(user, new_password, db)
    db.add(user)
    db.commit()
    log_event("password_changed", user_id=user.id, email=user.email)
    return render("settings.html", request, user=user, error=None, success="Password updated.")


@app.post("/settings/email", response_class=HTMLResponse)
def change_email(
    request: Request,
    current_password: str = Form(...),
    new_email: str = Form(...),
    db: Session = Depends(get_db),
):
    user, state = active_session_user(request, db)
    if not user:
        if state in {"disabled", "pending"}:
            return render("login.html", request, error="Unable to access account.", status_code=403)
        return RedirectResponse("/login", status_code=302)

    normalized_email = new_email.lower().strip()
    if not verify_password(current_password, user.password_hash):
        return render("settings.html", request, user=user, error="Current password is incorrect.", success=None, status_code=400)
    if not normalized_email:
        return render("settings.html", request, user=user, error="Email is required.", success=None, status_code=400)
    if db.query(User).filter(User.email == normalized_email, User.id != user.id).first():
        return render("settings.html", request, user=user, error="Email is already in use.", success=None, status_code=409)

    old_email = user.email
    user.email = normalized_email
    db.add(user)
    db.commit()
    log_event("email_changed", user_id=user.id, old_email=old_email, new_email=user.email)
    return render("settings.html", request, user=user, error=None, success="Email updated.")


@app.post("/settings/preferences", response_class=HTMLResponse)
def update_preferences(
    request: Request,
    theme: str = Form(...),
    text_size: str = Form(...),
    db: Session = Depends(get_db),
):
    user, state = active_session_user(request, db)
    if not user:
        if state in {"disabled", "pending"}:
            return render("login.html", request, error="Unable to access account.", status_code=403)
        return RedirectResponse("/login", status_code=302)

    normalized_theme = theme.strip().lower()
    normalized_text_size = text_size.strip().lower()
    if normalized_theme not in VALID_THEMES:
        return render("settings.html", request, user=user, error="Invalid theme.", success=None, status_code=400)
    if normalized_text_size not in VALID_TEXT_SIZES:
        return render("settings.html", request, user=user, error="Invalid text size.", success=None, status_code=400)

    user.theme = normalized_theme
    user.text_size = normalized_text_size
    db.add(user)
    db.commit()
    log_event("preferences_updated", user_id=user.id, theme=user.theme, text_size=user.text_size)
    return render("settings.html", request, user=user, error=None, success="Display settings updated.")


@app.post("/birds", response_class=HTMLResponse)
def create_bird(
    request: Request,
    bird_type: str = Form(...),
    sex: str = Form(...),
    band_number: str = Form(""),
    birth_date: str = Form(""),
    birth_place: str = Form(""),
    foreign_loft_owner_name: str = Form(""),
    pedigree: str = Form(""),
    bloodline: str = Form(""),
    special_colors: str = Form(""),
    features_markings: str = Form(""),
    racing_homer_notes: str = Form(""),
    family_tree_notes: str = Form(""),
    mate_band_number: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    normalized_type = bird_type.strip()
    normalized_sex = sex.strip()
    normalized_band = band_number.strip() or None
    if not normalized_type:
        return render("dashboard.html", request, **dashboard_context(admin, db, error="Bird type is required."), status_code=400)
    if not normalized_sex:
        return render("dashboard.html", request, **dashboard_context(admin, db, error="Bird sex is required."), status_code=400)
    if normalized_band and db.query(Bird).filter(Bird.band_number == normalized_band).first():
        return render("dashboard.html", request, **dashboard_context(admin, db, error="Band number already exists."), status_code=409)

    parsed_birth_date = None
    if birth_date.strip():
        try:
            parsed_birth_date = date.fromisoformat(birth_date.strip())
        except ValueError:
            return render(
                "dashboard.html",
                request,
                **dashboard_context(admin, db, error="Birth date must use YYYY-MM-DD format."),
                status_code=400,
            )

    bird = Bird(
        bird_type=normalized_type,
        sex=normalized_sex,
        band_number=normalized_band,
        birth_date=parsed_birth_date,
        birth_place=birth_place.strip() or None,
        foreign_loft_owner_name=foreign_loft_owner_name.strip() or None,
        pedigree=pedigree.strip() or None,
        bloodline=bloodline.strip() or None,
        special_colors=special_colors.strip() or None,
        features_markings=features_markings.strip() or None,
        racing_homer_notes=racing_homer_notes.strip() or None,
        family_tree_notes=family_tree_notes.strip() or None,
        mate_band_number=mate_band_number.strip() or None,
    )
    db.add(bird)
    db.commit()
    log_event("bird_created", user_id=admin.id, band_number=bird.band_number)
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/flights", response_class=HTMLResponse)
def create_flight_log(
    request: Request,
    bird_band_number: str = Form(""),
    flight_date: str = Form(...),
    release_location: str = Form(""),
    arrival_location: str = Form(""),
    distance_km: str = Form(""),
    duration_minutes: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    try:
        parsed_flight_date = date.fromisoformat(flight_date.strip())
    except ValueError:
        return render(
            "dashboard.html",
            request,
            **dashboard_context(admin, db, error="Flight date must use YYYY-MM-DD format."),
            status_code=400,
        )

    parsed_duration = None
    if duration_minutes.strip():
        if not duration_minutes.strip().isdigit():
            return render(
                "dashboard.html",
                request,
                **dashboard_context(admin, db, error="Duration must be a whole number of minutes."),
                status_code=400,
            )
        parsed_duration = int(duration_minutes.strip())

    band = bird_band_number.strip()
    linked_bird = db.query(Bird).filter(Bird.band_number == band).first() if band else None

    flight = FlightLog(
        bird_id=linked_bird.id if linked_bird else None,
        bird_band_number=band or None,
        flight_date=parsed_flight_date,
        release_location=release_location.strip() or None,
        arrival_location=arrival_location.strip() or None,
        distance_km=distance_km.strip() or None,
        duration_minutes=parsed_duration,
        notes=notes.strip() or None,
    )
    db.add(flight)
    db.commit()
    log_event("flight_log_created", user_id=admin.id, bird_band_number=flight.bird_band_number)
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)
    return render("admin.html", request, **admin_context(admin, db))


@app.post("/admin/users/{target_user_id}/approve", response_class=HTMLResponse)
def approve_user(target_user_id: int, request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    target_user = db.query(User).filter(User.id == target_user_id).first()
    if target_user and target_user.role == ROLE_READ_ONLY:
        target_user.is_approved = True
        target_user.approved_at = datetime.now(timezone.utc)
        db.add(target_user)
        db.commit()
        log_event("user_approved", admin_id=admin.id, target_user_id=target_user.id)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{target_user_id}/role", response_class=HTMLResponse)
def change_user_role(
    target_user_id: int,
    request: Request,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    normalized_role = role.strip().lower()
    if normalized_role not in {ROLE_ADMIN, ROLE_READ_ONLY}:
        return render("admin.html", request, **admin_context(admin, db, error="Invalid role."), status_code=400)

    target_user = db.query(User).filter(User.id == target_user_id).first()
    if not target_user:
        return RedirectResponse("/admin", status_code=302)

    if target_user.id == admin.id and normalized_role == ROLE_READ_ONLY:
        return render(
            "admin.html",
            request,
            **admin_context(admin, db, error="You cannot demote your own account from admin."),
            status_code=400,
        )

    target_user.role = normalized_role
    if normalized_role == ROLE_ADMIN:
        target_user.is_approved = True
    db.add(target_user)
    db.commit()
    log_event("user_role_changed", admin_id=admin.id, target_user_id=target_user.id, new_role=target_user.role)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{target_user_id}/status", response_class=HTMLResponse)
def change_user_status(
    target_user_id: int,
    request: Request,
    is_enabled: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    target_user = db.query(User).filter(User.id == target_user_id).first()
    if not target_user:
        return RedirectResponse("/admin", status_code=302)

    enabled_value = is_enabled.strip().lower() == "true"
    if target_user.id == admin.id and not enabled_value:
        return render(
            "admin.html",
            request,
            **admin_context(admin, db, error="You cannot disable your own account."),
            status_code=400,
        )

    target_user.is_enabled = enabled_value
    db.add(target_user)
    db.commit()
    log_event("user_status_changed", admin_id=admin.id, target_user_id=target_user.id, is_enabled=enabled_value)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/logs/export")
def export_logs(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_name = f"{timestamp}.zip"
    zip_path = LOG_DIR / zip_name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(LOG_DIR.glob("*")):
            if path.name == zip_name:
                continue
            if path.is_file() and path.suffix.lower() in {".log", ".txt", ".json"}:
                archive.write(path, arcname=path.name)

    log_event("logs_exported", admin_id=admin.id, zip_file=zip_name)
    return FileResponse(path=zip_path, media_type="application/zip", filename=zip_name)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
