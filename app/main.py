import os
import re
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import hash_password, verify_password
from .database import Base, SessionLocal, engine
from .models import Bird, PasswordHistory, User

load_dotenv()

app = FastAPI(title="jedLoft Web")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-secret-change-me"),
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

ROLE_ADMIN = "admin"
ROLE_READ_ONLY = "read_only"
PASSWORD_MIN_LENGTH = 6
PASSWORD_MIN_UPPERCASE = 2


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
        conn.execute(text("UPDATE users SET name = split_part(email, '@', 1) WHERE name IS NULL"))
        conn.execute(text("UPDATE users SET role = 'read_only' WHERE role IS NULL"))
        conn.execute(text("UPDATE users SET is_approved = TRUE WHERE is_approved IS NULL"))


def validate_password_policy(password: str) -> str | None:
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."

    uppercase_count = len(re.findall(r"[A-Z]", password))
    if uppercase_count < PASSWORD_MIN_UPPERCASE:
        return f"Password must include at least {PASSWORD_MIN_UPPERCASE} uppercase letters."

    return None


def get_recent_password_hashes(user: User, db: Session, limit: int = 2) -> list[str]:
    history_rows = (
        db.query(PasswordHistory)
        .filter(PasswordHistory.user_id == user.id)
        .order_by(PasswordHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [row.password_hash for row in history_rows]


def is_password_reused(user: User, new_password: str, db: Session) -> bool:
    if verify_password(new_password, user.password_hash):
        return True

    recent_hashes = get_recent_password_hashes(user, db, limit=2)
    return any(verify_password(new_password, old_hash) for old_hash in recent_hashes)


def set_user_password(user: User, new_password: str, db: Session) -> None:
    if user.password_hash:
        db.add(PasswordHistory(user_id=user.id, password_hash=user.password_hash))

    user.password_hash = hash_password(new_password)

    # Keep only the 2 most recent old password hashes.
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
            password_hash=hash_password(admin_password),
        )
        db.add(admin)
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_user_table_columns()
    bootstrap_default_admin()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None},
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password."},
            status_code=401,
        )

    if not user.is_approved:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Your account is pending admin approval."},
            status_code=403,
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "error": None},
    )


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
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Name is required."},
            status_code=400,
        )

    password_error = validate_password_policy(password)
    if password_error:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": password_error},
            status_code=400,
        )

    if db.query(User).filter(User.email == normalized_email).first():
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Email already registered."},
            status_code=409,
        )

    user = User(
        name=normalized_name,
        email=normalized_email,
        role=ROLE_READ_ONLY,
        is_approved=False,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Registration submitted. Wait for admin approval before login.",
        },
    )


def require_admin(request: Request, db: Session) -> User | None:
    user = current_user(request, db)
    if not user or user.role != ROLE_ADMIN:
        return None
    return user


def build_dashboard_context(
    user: User,
    db: Session,
    error: str | None = None,
    success: str | None = None,
) -> dict:
    pending_users = []
    all_users = []
    if user.role == ROLE_ADMIN:
        pending_users = (
            db.query(User)
            .filter(User.role == ROLE_READ_ONLY, User.is_approved.is_(False))
            .order_by(User.created_at.asc())
            .all()
        )
        all_users = db.query(User).order_by(User.created_at.asc()).all()

    birds = db.query(Bird).order_by(Bird.created_at.desc(), Bird.id.desc()).all()

    return {
        "request": None,
        "user": user,
        "pending_users": pending_users,
        "all_users": all_users,
        "birds": birds,
        "error": error,
        "success": success,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    context = build_dashboard_context(user, db)
    context["request"] = request
    return templates.TemplateResponse("dashboard.html", context)


@app.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not verify_password(current_password, user.password_hash):
        context = build_dashboard_context(user, db, error="Current password is incorrect.")
        context["request"] = request
        return templates.TemplateResponse(
            "dashboard.html",
            context,
            status_code=400,
        )

    password_error = validate_password_policy(new_password)
    if password_error:
        context = build_dashboard_context(user, db, error=password_error)
        context["request"] = request
        return templates.TemplateResponse(
            "dashboard.html",
            context,
            status_code=400,
        )

    if is_password_reused(user, new_password, db):
        context = build_dashboard_context(
            user,
            db,
            error="Password cannot match your current or last 2 passwords.",
        )
        context["request"] = request
        return templates.TemplateResponse(
            "dashboard.html",
            context,
            status_code=400,
        )

    set_user_password(user, new_password, db)
    db.add(user)
    db.commit()

    context = build_dashboard_context(user, db, success="Password updated.")
    context["request"] = request
    return templates.TemplateResponse("dashboard.html", context)


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
        context = build_dashboard_context(admin, db, error="Bird type is required.")
        context["request"] = request
        return templates.TemplateResponse("dashboard.html", context, status_code=400)

    if not normalized_sex:
        context = build_dashboard_context(admin, db, error="Bird sex is required.")
        context["request"] = request
        return templates.TemplateResponse("dashboard.html", context, status_code=400)

    if normalized_band and db.query(Bird).filter(Bird.band_number == normalized_band).first():
        context = build_dashboard_context(admin, db, error="Band number already exists.")
        context["request"] = request
        return templates.TemplateResponse("dashboard.html", context, status_code=409)

    parsed_birth_date = None
    if birth_date.strip():
        try:
            parsed_birth_date = date.fromisoformat(birth_date.strip())
        except ValueError:
            context = build_dashboard_context(admin, db, error="Birth date must use YYYY-MM-DD format.")
            context["request"] = request
            return templates.TemplateResponse("dashboard.html", context, status_code=400)

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
        family_tree_notes=family_tree_notes.strip() or None,
        mate_band_number=mate_band_number.strip() or None,
    )
    db.add(bird)
    db.commit()

    return RedirectResponse("/dashboard", status_code=302)


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

    return RedirectResponse("/dashboard", status_code=302)


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
        return RedirectResponse("/dashboard", status_code=302)

    target_user = db.query(User).filter(User.id == target_user_id).first()
    if not target_user:
        return RedirectResponse("/dashboard", status_code=302)

    if target_user.id == admin.id and normalized_role == ROLE_READ_ONLY:
        context = build_dashboard_context(admin, db, error="You cannot demote your own account from admin.")
        context["request"] = request
        return templates.TemplateResponse(
            "dashboard.html",
            context,
            status_code=400,
        )

    target_user.role = normalized_role
    if normalized_role == ROLE_ADMIN:
        target_user.is_approved = True
    db.add(target_user)
    db.commit()

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
