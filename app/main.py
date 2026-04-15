import logging
import os
import re
import random
import zipfile
from datetime import date, datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import hash_password, verify_password
from .database import Base, SessionLocal, engine
from .models import Bird, BirdShare, FlightLog, GroupMembership, OwnershipGroup, PasswordHistory, User

load_dotenv()

ROLE_ADMIN = "admin"
ROLE_READ_ONLY = "read_only"
PASSWORD_MIN_LENGTH = 6
PASSWORD_MIN_UPPERCASE = 2
VALID_THEMES = {"standard", "colorblind"}
VALID_TEXT_SIZES = {"small", "medium", "large"}
VALID_GROUP_PERMISSIONS = {"view", "edit", "none"}
VALID_SHARE_PERMISSIONS = {"view", "edit", "none"}

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

allowed_origins = [origin.strip() for origin in os.getenv("MOBILE_ALLOWED_ORIGINS", "*").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
basic_security = HTTPBasic(auto_error=False)


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
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_enabled BOOLEAN DEFAULT TRUE NOT NULL"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(32) DEFAULT 'standard' NOT NULL"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS text_size VARCHAR(16) DEFAULT 'medium' NOT NULL"))

    db = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            if not user.name:
                user.name = user.email.split("@", 1)[0]
            if not user.role:
                user.role = ROLE_READ_ONLY
            if user.is_approved is None:
                user.is_approved = True
            if user.is_enabled is None:
                user.is_enabled = True
            if not user.theme:
                user.theme = "standard"
            if not user.text_size:
                user.text_size = "medium"
            db.add(user)
        db.commit()
    finally:
        db.close()


def ensure_record_table_columns() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE birds ADD COLUMN IF NOT EXISTS racing_homer_notes VARCHAR(1500)"))
        conn.execute(text("ALTER TABLE birds ADD COLUMN IF NOT EXISTS ownership_group_id INTEGER"))
        conn.execute(text("ALTER TABLE birds ADD COLUMN IF NOT EXISTS owner_user_id INTEGER"))
        conn.execute(text("ALTER TABLE birds ADD COLUMN IF NOT EXISTS system_bird_id INTEGER"))
        conn.execute(text("ALTER TABLE birds ADD COLUMN IF NOT EXISTS user_bird_id VARCHAR(120)"))
        conn.execute(text("ALTER TABLE flight_logs ADD COLUMN IF NOT EXISTS ownership_group_id INTEGER"))


def next_system_bird_id(db: Session, reserved_ids: set[int] | None = None) -> int:
    existing_ids = {
        row[0]
        for row in db.query(Bird.system_bird_id).filter(Bird.system_bird_id.isnot(None)).all()
        if row[0] is not None
    }
    if reserved_ids:
        existing_ids.update(reserved_ids)
    candidate = random.randint(100000, 999999)
    while candidate in existing_ids:
        candidate = random.randint(100000, 999999)
    return candidate


def ensure_bird_system_ids() -> None:
    db = SessionLocal()
    try:
        birds = db.query(Bird).filter(Bird.system_bird_id.is_(None)).order_by(Bird.id.asc()).all()
        if not birds:
            return
        reserved_ids: set[int] = set()
        for bird in birds:
            bird.system_bird_id = next_system_bird_id(db, reserved_ids)
            reserved_ids.add(bird.system_bird_id)
            db.add(bird)
        db.commit()
    finally:
        db.close()


def parse_bird_birth_date(value: str) -> date | None:
    if not value.strip():
        return None
    return date.fromisoformat(value.strip())


def bird_as_payload(bird: Bird, permission: str | None = None) -> dict[str, object]:
    return {
        "id": bird.id,
        "owner_user_id": bird.owner_user_id,
        "system_bird_id": bird.system_bird_id,
        "user_bird_id": bird.user_bird_id,
        "ownership_group_id": bird.ownership_group_id,
        "permission": permission,
        "bird_type": bird.bird_type,
        "sex": bird.sex,
        "band_number": bird.band_number,
        "birth_date": bird.birth_date.isoformat() if bird.birth_date else None,
        "birth_place": bird.birth_place,
        "foreign_loft_owner_name": bird.foreign_loft_owner_name,
        "pedigree": bird.pedigree,
        "bloodline": bird.bloodline,
        "special_colors": bird.special_colors,
        "features_markings": bird.features_markings,
        "racing_homer_notes": bird.racing_homer_notes,
        "family_tree_notes": bird.family_tree_notes,
        "mate_band_number": bird.mate_band_number,
    }


def flight_as_payload(flight: FlightLog) -> dict[str, object]:
    return {
        "id": flight.id,
        "bird_id": flight.bird_id,
        "bird_band_number": flight.bird_band_number,
        "ownership_group_id": flight.ownership_group_id,
        "flight_date": flight.flight_date.isoformat(),
        "release_location": flight.release_location,
        "arrival_location": flight.arrival_location,
        "distance_km": flight.distance_km,
        "duration_minutes": flight.duration_minutes,
        "notes": flight.notes,
    }


def parse_optional_int(value: str) -> int | None:
    normalized = value.strip()
    if not normalized:
        return None
    if not normalized.isdigit():
        raise ValueError("Value must be a whole number.")
    return int(normalized)


def editable_bird_permission(user: User, bird: Bird, db: Session) -> str:
    group_permissions = group_permissions_for_user(user, db)
    share_permissions = compute_bird_share_permissions(user, db)
    return bird_permission_for_user(user, bird, group_permissions, share_permissions)


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
    ensure_record_table_columns()
    ensure_bird_system_ids()
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


def group_permissions_for_user(user: User, db: Session) -> dict[int, str]:
    if user.role == ROLE_ADMIN:
        return {group.id: "edit" for group in db.query(OwnershipGroup).all()}

    memberships = db.query(GroupMembership).filter(GroupMembership.user_id == user.id).all()
    return {membership.group_id: membership.permission for membership in memberships}


def can_view_group(user: User, group_id: int | None, db: Session) -> bool:
    if user.role == ROLE_ADMIN:
        return True
    if group_id is None:
        return False
    permissions = group_permissions_for_user(user, db)
    return permissions.get(group_id) in {"view", "edit"}


def can_edit_group(user: User, group_id: int | None, db: Session) -> bool:
    if user.role == ROLE_ADMIN:
        return True
    if group_id is None:
        return False
    permissions = group_permissions_for_user(user, db)
    return permissions.get(group_id) == "edit"


def permission_rank(permission: str) -> int:
    if permission == "edit":
        return 2
    if permission == "view":
        return 1
    return 0


def compute_bird_share_permissions(user: User, db: Session) -> dict[int, str]:
    if user.role == ROLE_ADMIN:
        return {bird_id: "edit" for (bird_id,) in db.query(Bird.id).all()}

    membership_permissions = group_permissions_for_user(user, db)
    group_ids = list(membership_permissions.keys())
    shares_query = db.query(BirdShare).filter(BirdShare.user_id == user.id)
    if group_ids:
        shares_query = shares_query.union(db.query(BirdShare).filter(BirdShare.group_id.in_(group_ids)))
    shares = shares_query.all()

    permission_by_bird: dict[int, str] = {}
    for share in shares:
        current = permission_by_bird.get(share.bird_id, "none")
        if permission_rank(share.permission) > permission_rank(current):
            permission_by_bird[share.bird_id] = share.permission
    return permission_by_bird


def bird_permission_for_user(
    user: User,
    bird: Bird,
    group_permissions: dict[int, str],
    share_permissions: dict[int, str],
) -> str:
    if user.role == ROLE_ADMIN:
        return "edit"
    if bird.owner_user_id == user.id:
        return "edit"

    shared_permission = share_permissions.get(bird.id)
    if shared_permission in {"edit", "view"}:
        return shared_permission

    group_permission = group_permissions.get(bird.ownership_group_id or -1)
    if group_permission == "edit":
        return "edit"
    if group_permission == "view":
        return "view"
    return "none"


def visible_birds_for_user(user: User, db: Session) -> tuple[list[Bird], dict[int, str]]:
    birds = db.query(Bird).order_by(Bird.created_at.desc(), Bird.id.desc()).all()
    if user.role == ROLE_ADMIN:
        return birds, {bird.id: "edit" for bird in birds}

    group_permissions = group_permissions_for_user(user, db)
    share_permissions = compute_bird_share_permissions(user, db)

    visible: list[Bird] = []
    permission_by_bird: dict[int, str] = {}
    for bird in birds:
        permission = bird_permission_for_user(user, bird, group_permissions, share_permissions)
        if permission != "none":
            visible.append(bird)
            permission_by_bird[bird.id] = permission
    return visible, permission_by_bird


def dashboard_context(user: User, db: Session, error: str | None = None, success: str | None = None) -> dict:
    all_groups = db.query(OwnershipGroup).order_by(OwnershipGroup.name.asc()).all()
    group_name_by_id = {group.id: group.name for group in all_groups}

    if user.role == ROLE_ADMIN:
        birds, bird_permissions = visible_birds_for_user(user, db)
        flight_logs = db.query(FlightLog).order_by(FlightLog.flight_date.desc(), FlightLog.id.desc()).all()
        visible_groups = all_groups
        editable_groups = all_groups
    else:
        permissions = group_permissions_for_user(user, db)
        view_ids = [group_id for group_id, permission in permissions.items() if permission in {"view", "edit"}]
        edit_ids = [group_id for group_id, permission in permissions.items() if permission == "edit"]

        birds, bird_permissions = visible_birds_for_user(user, db)

        if view_ids:
            flight_logs = (
                db.query(FlightLog)
                .filter(FlightLog.ownership_group_id.in_(view_ids))
                .order_by(FlightLog.flight_date.desc(), FlightLog.id.desc())
                .all()
            )
            visible_groups = [group for group in all_groups if group.id in view_ids]
            editable_groups = [group for group in all_groups if group.id in edit_ids]
        else:
            birds = [bird for bird in birds if bird_permissions.get(bird.id) in {"view", "edit"}]
            flight_logs = []
            visible_groups = []
            editable_groups = []

    shareable_users = (
        db.query(User)
        .filter(User.is_approved.is_(True), User.is_enabled.is_(True), User.id != user.id)
        .order_by(User.email.asc())
        .all()
    )

    return {
        "user": user,
        "birds": birds,
        "bird_permissions": bird_permissions,
        "flight_logs": flight_logs,
        "ownership_groups": visible_groups,
        "editable_groups": editable_groups,
        "can_edit_records": len(editable_groups) > 0,
        "shareable_users": shareable_users,
        "group_name_by_id": group_name_by_id,
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
    ownership_groups = db.query(OwnershipGroup).order_by(OwnershipGroup.name.asc()).all()

    memberships = (
        db.query(GroupMembership, User, OwnershipGroup)
        .join(User, GroupMembership.user_id == User.id)
        .join(OwnershipGroup, GroupMembership.group_id == OwnershipGroup.id)
        .order_by(User.email.asc(), OwnershipGroup.name.asc())
        .all()
    )

    membership_rows = [
        {
            "membership_id": membership.id,
            "user_id": user_row.id,
            "user_email": user_row.email,
            "group_id": group.id,
            "group_name": group.name,
            "permission": membership.permission,
        }
        for membership, user_row, group in memberships
    ]

    return {
        "user": user,
        "pending_users": pending_users,
        "all_users": all_users,
        "ownership_groups": ownership_groups,
        "membership_rows": membership_rows,
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


def mobile_user_from_credentials(
    credentials: HTTPBasicCredentials | None,
    db: Session,
) -> User | None:
    if not credentials:
        return None
    normalized_email = credentials.username.lower().strip()
    user = db.query(User).filter(User.email == normalized_email).first()
    if not user:
        return None
    if not user.is_enabled or not user.is_approved:
        return None
    if not verify_password(credentials.password, user.password_hash):
        return None
    return user


@app.get("/android-client", response_class=HTMLResponse)
def android_client_page(request: Request):
    return templates.TemplateResponse("android_client.html", {"request": request, "nav_user": None, "theme_class": "", "text_size_class": ""})


@app.get("/api/mobile/health")
def mobile_health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "jedLoft", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.post("/api/mobile/login")
def mobile_login(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    normalized_email = email.lower().strip()
    user = db.query(User).filter(User.email == normalized_email).first()
    if not user or not verify_password(password, user.password_hash):
        return JSONResponse({"ok": False, "error": "Invalid credentials."}, status_code=401)
    if not user.is_enabled:
        return JSONResponse({"ok": False, "error": "Account is disabled."}, status_code=403)
    if not user.is_approved:
        return JSONResponse({"ok": False, "error": "Account is pending approval."}, status_code=403)

    return JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
            },
        }
    )


@app.get("/api/mobile/sync")
def mobile_sync(
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = mobile_user_from_credentials(credentials, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)

    birds, permission_by_bird = visible_birds_for_user(user, db)
    bird_ids = [bird.id for bird in birds]
    all_groups = db.query(OwnershipGroup).order_by(OwnershipGroup.name.asc()).all()
    group_permissions = group_permissions_for_user(user, db)
    visible_groups = [group for group in all_groups if user.role == ROLE_ADMIN or group.id in group_permissions]
    editable_groups = [group for group in visible_groups if user.role == ROLE_ADMIN or group_permissions.get(group.id) == "edit"]
    flights_query = db.query(FlightLog)
    if user.role != ROLE_ADMIN:
        group_ids = list(group_permissions.keys())
        filters = []
        if bird_ids:
            filters.append(FlightLog.bird_id.in_(bird_ids))
        if group_ids:
            filters.append(FlightLog.ownership_group_id.in_(group_ids))
        if filters:
            flights_query = flights_query.filter(or_(*filters))
        else:
            flights_query = flights_query.filter(FlightLog.id == -1)
    flight_logs = flights_query.order_by(FlightLog.flight_date.desc(), FlightLog.id.desc()).all()

    payload = {
        "ok": True,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "ownership_groups": [{"id": group.id, "name": group.name} for group in visible_groups],
        "editable_groups": [{"id": group.id, "name": group.name} for group in editable_groups],
        "can_edit_records": bool(editable_groups),
        "birds": [
            {
                "id": bird.id,
                "system_bird_id": bird.system_bird_id,
                "user_bird_id": bird.user_bird_id,
                "ownership_group_id": bird.ownership_group_id,
                "permission": permission_by_bird.get(bird.id, "none"),
                "bird_type": bird.bird_type,
                "sex": bird.sex,
                "band_number": bird.band_number,
                "birth_date": bird.birth_date.isoformat() if bird.birth_date else None,
                "birth_place": bird.birth_place,
                "foreign_loft_owner_name": bird.foreign_loft_owner_name,
                "pedigree": bird.pedigree,
                "bloodline": bird.bloodline,
                "special_colors": bird.special_colors,
                "features_markings": bird.features_markings,
                "racing_homer_notes": bird.racing_homer_notes,
                "family_tree_notes": bird.family_tree_notes,
                "mate_band_number": bird.mate_band_number,
            }
            for bird in birds
        ],
        "flight_logs": [
            {
                "id": flight.id,
                "bird_id": flight.bird_id,
                "bird_band_number": flight.bird_band_number,
                "ownership_group_id": flight.ownership_group_id,
                "flight_date": flight.flight_date.isoformat(),
                "release_location": flight.release_location,
                "arrival_location": flight.arrival_location,
                "distance_km": flight.distance_km,
                "duration_minutes": flight.duration_minutes,
                "notes": flight.notes,
            }
            for flight in flight_logs
        ],
    }
    return JSONResponse(payload)


@app.post("/api/mobile/birds")
def mobile_create_bird(
    ownership_group_id: str = Form(...),
    user_bird_id: str = Form(...),
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
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = mobile_user_from_credentials(credentials, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)

    try:
        group_id = parse_optional_int(ownership_group_id)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Ownership group is required."}, status_code=400)
    if group_id is None:
        return JSONResponse({"ok": False, "error": "Ownership group is required."}, status_code=400)

    if not can_edit_group(user, group_id, db):
        return JSONResponse({"ok": False, "error": "You do not have edit access to this ownership group."}, status_code=403)

    normalized_type = bird_type.strip()
    normalized_sex = sex.strip()
    normalized_user_bird_id = user_bird_id.strip()
    normalized_band = band_number.strip() or None
    if not normalized_type:
        return JSONResponse({"ok": False, "error": "Bird type is required."}, status_code=400)
    if not normalized_sex:
        return JSONResponse({"ok": False, "error": "Bird sex is required."}, status_code=400)
    if not normalized_user_bird_id:
        return JSONResponse({"ok": False, "error": "User bird ID is required."}, status_code=400)
    if normalized_band and db.query(Bird).filter(Bird.band_number == normalized_band).first():
        return JSONResponse({"ok": False, "error": "Band number already exists."}, status_code=409)

    try:
        parsed_birth_date = parse_bird_birth_date(birth_date)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Birth date must use YYYY-MM-DD format."}, status_code=400)

    bird = Bird(
        owner_user_id=user.id,
        system_bird_id=next_system_bird_id(db),
        user_bird_id=normalized_user_bird_id,
        ownership_group_id=group_id,
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
    log_event("mobile_bird_created", user_id=user.id, bird_id=bird.id, group_id=group_id)
    return JSONResponse({"ok": True, "bird": bird_as_payload(bird, permission="edit")}, status_code=201)


@app.patch("/api/mobile/birds/{bird_id}")
def mobile_update_bird(
    bird_id: int,
    ownership_group_id: str = Form(""),
    user_bird_id: str = Form(""),
    bird_type: str = Form(""),
    sex: str = Form(""),
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
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = mobile_user_from_credentials(credentials, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)

    bird = db.query(Bird).filter(Bird.id == bird_id).first()
    if not bird:
        return JSONResponse({"ok": False, "error": "Bird not found."}, status_code=404)

    if editable_bird_permission(user, bird, db) != "edit":
        return JSONResponse({"ok": False, "error": "You do not have edit access to this bird."}, status_code=403)

    if ownership_group_id.strip():
        try:
            new_group_id = parse_optional_int(ownership_group_id)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid ownership group."}, status_code=400)
        if new_group_id is None:
            return JSONResponse({"ok": False, "error": "Invalid ownership group."}, status_code=400)
        if not can_edit_group(user, new_group_id, db):
            return JSONResponse({"ok": False, "error": "You do not have edit access to that ownership group."}, status_code=403)
        bird.ownership_group_id = new_group_id

    if user_bird_id.strip():
        bird.user_bird_id = user_bird_id.strip()
    if bird_type.strip():
        bird.bird_type = bird_type.strip()
    if sex.strip():
        bird.sex = sex.strip()
    if band_number.strip():
        existing_bird = db.query(Bird).filter(Bird.band_number == band_number.strip(), Bird.id != bird.id).first()
        if existing_bird:
            return JSONResponse({"ok": False, "error": "Band number already exists."}, status_code=409)
        bird.band_number = band_number.strip()
    if birth_date.strip():
        try:
            bird.birth_date = parse_bird_birth_date(birth_date)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Birth date must use YYYY-MM-DD format."}, status_code=400)
    if birth_place.strip():
        bird.birth_place = birth_place.strip()
    if foreign_loft_owner_name.strip():
        bird.foreign_loft_owner_name = foreign_loft_owner_name.strip()
    if pedigree.strip():
        bird.pedigree = pedigree.strip()
    if bloodline.strip():
        bird.bloodline = bloodline.strip()
    if special_colors.strip():
        bird.special_colors = special_colors.strip()
    if features_markings.strip():
        bird.features_markings = features_markings.strip()
    if racing_homer_notes.strip():
        bird.racing_homer_notes = racing_homer_notes.strip()
    if family_tree_notes.strip():
        bird.family_tree_notes = family_tree_notes.strip()
    if mate_band_number.strip():
        bird.mate_band_number = mate_band_number.strip()

    db.add(bird)
    db.commit()
    log_event("mobile_bird_updated", user_id=user.id, bird_id=bird.id)
    return JSONResponse({"ok": True, "bird": bird_as_payload(bird, permission="edit")})


@app.post("/api/mobile/flights")
def mobile_create_flight(
    ownership_group_id: str = Form(...),
    bird_id: str = Form(""),
    bird_band_number: str = Form(""),
    flight_date: str = Form(...),
    release_location: str = Form(""),
    arrival_location: str = Form(""),
    distance_km: str = Form(""),
    duration_minutes: str = Form(""),
    notes: str = Form(""),
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = mobile_user_from_credentials(credentials, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)

    try:
        group_id = parse_optional_int(ownership_group_id)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Ownership group is required."}, status_code=400)
    if group_id is None:
        return JSONResponse({"ok": False, "error": "Ownership group is required."}, status_code=400)
    if not can_edit_group(user, group_id, db):
        return JSONResponse({"ok": False, "error": "You do not have edit access to this ownership group."}, status_code=403)

    try:
        parsed_flight_date = date.fromisoformat(flight_date.strip())
    except ValueError:
        return JSONResponse({"ok": False, "error": "Flight date must use YYYY-MM-DD format."}, status_code=400)

    parsed_duration = None
    if duration_minutes.strip():
        try:
            parsed_duration = parse_optional_int(duration_minutes)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Duration must be a whole number of minutes."}, status_code=400)

    linked_bird = None
    if bird_id.strip():
        if not bird_id.strip().isdigit():
            return JSONResponse({"ok": False, "error": "Bird ID must be numeric."}, status_code=400)
        linked_bird = db.query(Bird).filter(Bird.id == int(bird_id.strip())).first()
        if not linked_bird:
            return JSONResponse({"ok": False, "error": "Bird not found."}, status_code=404)
    elif bird_band_number.strip():
        linked_bird = db.query(Bird).filter(Bird.band_number == bird_band_number.strip()).first()

    if linked_bird and linked_bird.ownership_group_id != group_id:
        return JSONResponse({"ok": False, "error": "Linked bird does not belong to the selected ownership group."}, status_code=400)

    flight = FlightLog(
        ownership_group_id=group_id,
        bird_id=linked_bird.id if linked_bird else None,
        bird_band_number=bird_band_number.strip() or (linked_bird.band_number if linked_bird else None),
        flight_date=parsed_flight_date,
        release_location=release_location.strip() or None,
        arrival_location=arrival_location.strip() or None,
        distance_km=distance_km.strip() or None,
        duration_minutes=parsed_duration,
        notes=notes.strip() or None,
    )
    db.add(flight)
    db.commit()
    log_event("mobile_flight_created", user_id=user.id, flight_id=flight.id, group_id=group_id)
    return JSONResponse({"ok": True, "flight": flight_as_payload(flight)}, status_code=201)


@app.patch("/api/mobile/flights/{flight_id}")
def mobile_update_flight(
    flight_id: int,
    ownership_group_id: str = Form(""),
    bird_id: str = Form(""),
    bird_band_number: str = Form(""),
    flight_date: str = Form(""),
    release_location: str = Form(""),
    arrival_location: str = Form(""),
    distance_km: str = Form(""),
    duration_minutes: str = Form(""),
    notes: str = Form(""),
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = mobile_user_from_credentials(credentials, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Unauthorized."}, status_code=401)

    flight = db.query(FlightLog).filter(FlightLog.id == flight_id).first()
    if not flight:
        return JSONResponse({"ok": False, "error": "Flight log not found."}, status_code=404)

    if flight.ownership_group_id is not None and not can_edit_group(user, flight.ownership_group_id, db):
        return JSONResponse({"ok": False, "error": "You do not have edit access to this flight log."}, status_code=403)

    if ownership_group_id.strip():
        try:
            new_group_id = parse_optional_int(ownership_group_id)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid ownership group."}, status_code=400)
        if new_group_id is None:
            return JSONResponse({"ok": False, "error": "Invalid ownership group."}, status_code=400)
        if not can_edit_group(user, new_group_id, db):
            return JSONResponse({"ok": False, "error": "You do not have edit access to that ownership group."}, status_code=403)
        flight.ownership_group_id = new_group_id

    if bird_id.strip():
        if not bird_id.strip().isdigit():
            return JSONResponse({"ok": False, "error": "Bird ID must be numeric."}, status_code=400)
        linked_bird = db.query(Bird).filter(Bird.id == int(bird_id.strip())).first()
        if not linked_bird:
            return JSONResponse({"ok": False, "error": "Bird not found."}, status_code=404)
        if flight.ownership_group_id and linked_bird.ownership_group_id != flight.ownership_group_id:
            return JSONResponse({"ok": False, "error": "Linked bird does not belong to the selected ownership group."}, status_code=400)
        flight.bird_id = linked_bird.id
        flight.bird_band_number = linked_bird.band_number

    if bird_band_number.strip():
        flight.bird_band_number = bird_band_number.strip()
    if flight_date.strip():
        try:
            flight.flight_date = date.fromisoformat(flight_date.strip())
        except ValueError:
            return JSONResponse({"ok": False, "error": "Flight date must use YYYY-MM-DD format."}, status_code=400)
    if release_location.strip():
        flight.release_location = release_location.strip()
    if arrival_location.strip():
        flight.arrival_location = arrival_location.strip()
    if distance_km.strip():
        flight.distance_km = distance_km.strip()
    if duration_minutes.strip():
        try:
            flight.duration_minutes = parse_optional_int(duration_minutes)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Duration must be a whole number of minutes."}, status_code=400)
    if notes.strip():
        flight.notes = notes.strip()

    db.add(flight)
    db.commit()
    log_event("mobile_flight_updated", user_id=user.id, flight_id=flight.id)
    return JSONResponse({"ok": True, "flight": flight_as_payload(flight)})


@app.post("/birds", response_class=HTMLResponse)
def create_bird(
    request: Request,
    ownership_group_id: str = Form(""),
    user_bird_id: str = Form(...),
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
    user, _ = active_session_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not ownership_group_id.strip().isdigit():
        return render("dashboard.html", request, **dashboard_context(user, db, error="Ownership group is required."), status_code=400)
    group_id = int(ownership_group_id)

    group = db.query(OwnershipGroup).filter(OwnershipGroup.id == group_id).first()
    if not group:
        return render("dashboard.html", request, **dashboard_context(user, db, error="Ownership group was not found."), status_code=404)

    if not can_edit_group(user, group_id, db):
        return render("dashboard.html", request, **dashboard_context(user, db, error="You do not have edit access to this ownership group."), status_code=403)

    normalized_type = bird_type.strip()
    normalized_sex = sex.strip()
    normalized_user_bird_id = user_bird_id.strip()
    normalized_band = band_number.strip() or None
    if not normalized_type:
        return render("dashboard.html", request, **dashboard_context(user, db, error="Bird type is required."), status_code=400)
    if not normalized_sex:
        return render("dashboard.html", request, **dashboard_context(user, db, error="Bird sex is required."), status_code=400)
    if not normalized_user_bird_id:
        return render("dashboard.html", request, **dashboard_context(user, db, error="User bird ID is required."), status_code=400)
    if normalized_band and db.query(Bird).filter(Bird.band_number == normalized_band).first():
        return render("dashboard.html", request, **dashboard_context(user, db, error="Band number already exists."), status_code=409)

    parsed_birth_date = None
    if birth_date.strip():
        try:
            parsed_birth_date = parse_bird_birth_date(birth_date)
        except ValueError:
            return render(
                "dashboard.html",
                request,
                **dashboard_context(user, db, error="Birth date must use YYYY-MM-DD format."),
                status_code=400,
            )

    bird = Bird(
        owner_user_id=user.id,
        system_bird_id=next_system_bird_id(db),
        user_bird_id=normalized_user_bird_id,
        ownership_group_id=group_id,
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
    log_event("bird_created", user_id=user.id, band_number=bird.band_number, group_id=group_id)
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/birds/{bird_id}/share", response_class=HTMLResponse)
def share_bird(
    bird_id: int,
    request: Request,
    target_user_id: str = Form(...),
    permission: str = Form(...),
    db: Session = Depends(get_db),
):
    user, state = active_session_user(request, db)
    if not user:
        if state in {"disabled", "pending"}:
            return render("login.html", request, error="Unable to access account.", status_code=403)
        return RedirectResponse("/login", status_code=302)

    bird = db.query(Bird).filter(Bird.id == bird_id).first()
    if not bird:
        return render("dashboard.html", request, **dashboard_context(user, db, error="Bird not found."), status_code=404)

    if user.role != ROLE_ADMIN and bird.owner_user_id != user.id:
        return render(
            "dashboard.html",
            request,
            **dashboard_context(user, db, error="Only the owner or an admin can manage bird sharing."),
            status_code=403,
        )

    if not target_user_id.isdigit():
        return render("dashboard.html", request, **dashboard_context(user, db, error="Target user is required."), status_code=400)

    normalized_permission = permission.strip().lower()
    if normalized_permission not in VALID_SHARE_PERMISSIONS:
        return render("dashboard.html", request, **dashboard_context(user, db, error="Invalid share permission."), status_code=400)

    target_user = db.query(User).filter(User.id == int(target_user_id)).first()
    if not target_user:
        return render("dashboard.html", request, **dashboard_context(user, db, error="Target user not found."), status_code=404)
    if target_user.id == (bird.owner_user_id or -1):
        return render("dashboard.html", request, **dashboard_context(user, db, error="Owner already has full access."), status_code=400)

    existing_share = (
        db.query(BirdShare)
        .filter(BirdShare.bird_id == bird.id, BirdShare.user_id == target_user.id, BirdShare.group_id.is_(None))
        .first()
    )

    if normalized_permission == "none":
        if existing_share:
            db.delete(existing_share)
            db.commit()
        return RedirectResponse("/dashboard", status_code=302)

    if existing_share:
        existing_share.permission = normalized_permission
        db.add(existing_share)
    else:
        db.add(BirdShare(bird_id=bird.id, user_id=target_user.id, permission=normalized_permission))
    db.commit()
    log_event(
        "bird_share_updated",
        actor_user_id=user.id,
        bird_id=bird.id,
        target_user_id=target_user.id,
        permission=normalized_permission,
    )
    return RedirectResponse("/dashboard", status_code=302)


@app.post("/flights", response_class=HTMLResponse)
def create_flight_log(
    request: Request,
    ownership_group_id: str = Form(""),
    bird_band_number: str = Form(""),
    flight_date: str = Form(...),
    release_location: str = Form(""),
    arrival_location: str = Form(""),
    distance_km: str = Form(""),
    duration_minutes: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user, _ = active_session_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not ownership_group_id.strip().isdigit():
        return render("dashboard.html", request, **dashboard_context(user, db, error="Ownership group is required."), status_code=400)
    group_id = int(ownership_group_id)

    group = db.query(OwnershipGroup).filter(OwnershipGroup.id == group_id).first()
    if not group:
        return render("dashboard.html", request, **dashboard_context(user, db, error="Ownership group was not found."), status_code=404)

    if not can_edit_group(user, group_id, db):
        return render("dashboard.html", request, **dashboard_context(user, db, error="You do not have edit access to this ownership group."), status_code=403)

    try:
        parsed_flight_date = date.fromisoformat(flight_date.strip())
    except ValueError:
        return render(
            "dashboard.html",
            request,
            **dashboard_context(user, db, error="Flight date must use YYYY-MM-DD format."),
            status_code=400,
        )

    parsed_duration = None
    if duration_minutes.strip():
        if not duration_minutes.strip().isdigit():
            return render(
                "dashboard.html",
                request,
                **dashboard_context(user, db, error="Duration must be a whole number of minutes."),
                status_code=400,
            )
        parsed_duration = int(duration_minutes.strip())

    band = bird_band_number.strip()
    linked_bird = db.query(Bird).filter(Bird.band_number == band).first() if band else None
    if linked_bird and linked_bird.ownership_group_id != group_id:
        return render(
            "dashboard.html",
            request,
            **dashboard_context(user, db, error="Linked bird does not belong to the selected ownership group."),
            status_code=400,
        )

    flight = FlightLog(
        ownership_group_id=group_id,
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
    log_event("flight_log_created", user_id=user.id, bird_band_number=flight.bird_band_number, group_id=group_id)
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)
    return render("admin.html", request, **admin_context(admin, db))


@app.post("/admin/groups", response_class=HTMLResponse)
def create_ownership_group(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    normalized_name = name.strip()
    if not normalized_name:
        return render("admin.html", request, **admin_context(admin, db, error="Group name is required."), status_code=400)

    existing = db.query(OwnershipGroup).filter(OwnershipGroup.name == normalized_name).first()
    if existing:
        return render("admin.html", request, **admin_context(admin, db, error="Ownership group already exists."), status_code=409)

    group = OwnershipGroup(name=normalized_name)
    db.add(group)
    db.commit()
    log_event("ownership_group_created", admin_id=admin.id, group_name=normalized_name)
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/groups/memberships", response_class=HTMLResponse)
def upsert_group_membership(
    request: Request,
    user_id: str = Form(...),
    group_id: str = Form(...),
    permission: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_admin(request, db)
    if not admin:
        return RedirectResponse("/dashboard", status_code=302)

    if not user_id.isdigit() or not group_id.isdigit():
        return render("admin.html", request, **admin_context(admin, db, error="User and group are required."), status_code=400)

    normalized_permission = permission.strip().lower()
    if normalized_permission not in VALID_GROUP_PERMISSIONS:
        return render("admin.html", request, **admin_context(admin, db, error="Invalid membership permission."), status_code=400)

    target_user = db.query(User).filter(User.id == int(user_id)).first()
    target_group = db.query(OwnershipGroup).filter(OwnershipGroup.id == int(group_id)).first()
    if not target_user or not target_group:
        return render("admin.html", request, **admin_context(admin, db, error="User or group not found."), status_code=404)

    membership = (
        db.query(GroupMembership)
        .filter(GroupMembership.user_id == target_user.id, GroupMembership.group_id == target_group.id)
        .first()
    )

    if normalized_permission == "none":
        if membership:
            db.delete(membership)
            db.commit()
            log_event("group_membership_removed", admin_id=admin.id, target_user_id=target_user.id, group_id=target_group.id)
        return RedirectResponse("/admin", status_code=302)

    if membership:
        membership.permission = normalized_permission
        db.add(membership)
    else:
        membership = GroupMembership(user_id=target_user.id, group_id=target_group.id, permission=normalized_permission)
        db.add(membership)
    db.commit()

    log_event(
        "group_membership_updated",
        admin_id=admin.id,
        target_user_id=target_user.id,
        group_id=target_group.id,
        permission=normalized_permission,
    )
    return RedirectResponse("/admin", status_code=302)


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
