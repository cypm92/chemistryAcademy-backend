from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.orm import Session

from . import models, schemas
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .notifications import notify_admin_of_booking_request, notify_admin_of_contact_request, notify_student_of_booking_decision
from .security import admin_user, create_token, current_user, hash_password, verify_password
from .storage import storage


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def seed_admin() -> None:
    with SessionLocal() as db:
        existing = db.scalar(select(models.User).where(models.User.email == settings.admin_email.lower()))
        if not existing:
            db.add(models.User(name=settings.admin_name, email=settings.admin_email.lower(),
                               password_hash=hash_password(settings.admin_password), role="admin"))
            db.commit()


def ensure_default_folder() -> None:
    with SessionLocal() as db:
        existing = db.scalar(select(models.Folder).where(
            models.Folder.name == "Sin clasificar",
            models.Folder.parent_id.is_(None),
        ))
        if not existing:
            db.add(models.Folder(name="Sin clasificar"))
            db.commit()


def ensure_user_avatar_columns() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("users")}
    with engine.begin() as connection:
        if "avatar_key" not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN avatar_key VARCHAR(255)"))
        if "avatar_content_type" not in columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN avatar_content_type VARCHAR(100)"))


def ensure_booking_history_column() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("bookings")}
    if "is_historical" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN is_historical BOOLEAN NOT NULL DEFAULT FALSE"))


def ensure_guest_booking_columns() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("bookings")}
    with engine.begin() as connection:
        if "guest_name" not in columns:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN guest_name VARCHAR(120)"))
        if "guest_email" not in columns:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN guest_email VARCHAR(255)"))


def ensure_booking_comment_column() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("bookings")}
    if "admin_comment" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN admin_comment TEXT NOT NULL DEFAULT ''"))


def ensure_class_attachment_column() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("materials")}
    with engine.begin() as connection:
        if "is_class_attachment" not in columns:
            connection.execute(text("ALTER TABLE materials ADD COLUMN is_class_attachment BOOLEAN NOT NULL DEFAULT FALSE"))
        connection.execute(text("UPDATE materials SET is_class_attachment = TRUE WHERE id IN (SELECT material_id FROM booking_materials)"))


def ensure_folder_color_column() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("folders")}
    if "color" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE folders ADD COLUMN color VARCHAR(7)"))


def ensure_default_theme() -> None:
    with SessionLocal() as db:
        if not db.get(models.AppSetting, "primary_color"):
            db.add(models.AppSetting(key="primary_color", value="#1D6B4F"))
            db.commit()


def ensure_app_setting_text_column() -> None:
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE app_settings ALTER COLUMN value TYPE TEXT"))


HOME_CONTENT_DEFAULT = {
    "hero_title": "La química no se memoriza. Se entiende.",
    "hero_description": "Clases particulares y en grupo reducido, online, para 2º de Bachillerato y universitarios de química, física y matemáticas.",
    "concerns": ["Estudio horas y luego en el examen me quedo en blanco.", "Entiendo la teoría, pero no sé por dónde empezar el problema.", "Apruebo raspado y necesito subir la nota para entrar donde quiero.", "Llevo dos convocatorias con orgánica y no hay manera.", "Voy perdido y no sé si voy a llegar a la PAU."],
    "particular_price_bach": "22 €", "particular_price_uni": "27 €",
    "about_title": "Hola, soy Be Ciencia.",
    "about_quote": "La química deja de ser un muro cuando alguien te enseña a mirarla bien.",
    "about_text": "Clases cercanas, claras y pensadas para que entiendas lo que haces antes de memorizarlo.",
    "newsletter_title": "Un ejercicio resuelto en tu correo, cada semana.",
    "newsletter_text": "Un problema tipo examen explicado paso a paso, con el error que casi todo el mundo comete en él.",
    "contact_title": "Hablamos.", "contact_text": "Escríbeme y reservamos tu clase de prueba gratuita. Te contesto yo, no un formulario automático.",
    "whatsapp": "", "email": "", "instagram": "",
    "closing_title": "Antes de irte, llévate el método.",
    "closing_text": "Empieza con una clase de prueba gratuita y descubre una forma distinta de estudiar química.",
}


def home_content(db: Session) -> dict:
    setting = db.get(models.AppSetting, "home_content")
    if not setting:
        return HOME_CONTENT_DEFAULT
    try:
        saved = json.loads(setting.value)
        return {**HOME_CONTENT_DEFAULT, **saved}
    except (TypeError, json.JSONDecodeError):
        return HOME_CONTENT_DEFAULT


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    ensure_user_avatar_columns()
    ensure_booking_history_column()
    ensure_guest_booking_columns()
    ensure_booking_comment_column()
    ensure_class_attachment_column()
    ensure_folder_color_column()
    ensure_app_setting_text_column()
    ensure_default_theme()
    seed_admin()
    ensure_default_folder()
    yield


app = FastAPI(title="Chemistry Academy API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/theme", response_model=schemas.ThemeOut)
def theme(db: Session = Depends(get_db)):
    setting = db.get(models.AppSetting, "primary_color")
    return {"primary_color": setting.value if setting else "#1D6B4F"}


@app.get("/branding", response_model=schemas.BrandingOut)
def branding(db: Session = Depends(get_db)):
    color = db.get(models.AppSetting, "primary_color")
    logo = db.get(models.AppSetting, "logo_key")
    return {"primary_color": color.value if color else "#1D6B4F", "has_custom_logo": bool(logo and logo.value)}


@app.get("/home-content")
def get_home_content(db: Session = Depends(get_db)):
    return {"content": home_content(db)}


@app.patch("/admin/home-content")
def update_home_content(data: schemas.HomeContentUpdate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    serialized = json.dumps(data.content, ensure_ascii=False)
    if len(serialized) > 20000:
        raise HTTPException(400, "El contenido de la portada es demasiado extenso")
    setting = db.get(models.AppSetting, "home_content")
    if not setting:
        setting = models.AppSetting(key="home_content", value=serialized)
        db.add(setting)
    else:
        setting.value = serialized
    db.commit()
    return {"content": home_content(db)}


@app.get("/branding/logo")
def branding_logo(db: Session = Depends(get_db)):
    key = db.get(models.AppSetting, "logo_key")
    content_type = db.get(models.AppSetting, "logo_content_type")
    if not key or not key.value:
        raise HTTPException(404, "No hay un logo personalizado")
    path = storage.path(key.value)
    if not path.exists():
        raise HTTPException(404, "El logo no está disponible")
    return FileResponse(path, media_type=content_type.value if content_type else "image/png",
                        headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"})


@app.patch("/admin/theme", response_model=schemas.ThemeOut)
def update_theme(data: schemas.ThemeUpdate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    setting = db.get(models.AppSetting, "primary_color")
    if not setting:
        setting = models.AppSetting(key="primary_color", value=data.primary_color.upper())
        db.add(setting)
    else:
        setting.value = data.primary_color.upper()
    db.commit(); db.refresh(setting)
    return {"primary_color": setting.value}


@app.put("/admin/branding/logo", response_model=schemas.BrandingOut)
async def update_branding_logo(file: UploadFile = File(...), _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    content_type = (file.content_type or "").lower()
    suffix = Path(file.filename or "").suffix.lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"} or suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "Selecciona un logo en formato JPG, PNG o WebP")
    if file.size is not None and file.size > 5 * 1024 * 1024:
        raise HTTPException(400, "El logo no puede superar 5 MB")
    key, size = await storage.save(file)
    if size > 5 * 1024 * 1024:
        storage.delete(key)
        raise HTTPException(400, "El logo no puede superar 5 MB")
    old_key = db.get(models.AppSetting, "logo_key")
    old_value = old_key.value if old_key else None
    if not old_key:
        old_key = models.AppSetting(key="logo_key", value=key)
        db.add(old_key)
    else:
        old_key.value = key
    content = db.get(models.AppSetting, "logo_content_type")
    if not content:
        content = models.AppSetting(key="logo_content_type", value=content_type)
        db.add(content)
    else:
        content.value = content_type
    db.commit()
    if old_value:
        storage.delete(old_value)
    color = db.get(models.AppSetting, "primary_color")
    return {"primary_color": color.value if color else "#1D6B4F", "has_custom_logo": True}


@app.post("/auth/register", response_model=schemas.UserOut, status_code=201)
def register(data: schemas.UserCreate, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(models.User).where(models.User.email == email)):
        raise HTTPException(400, "El email ya está registrado")
    user = models.User(name=data.name, email=email, password_hash=hash_password(data.password), role="guest")
    db.add(user); db.commit(); db.refresh(user)
    return user


@app.post("/auth/login", response_model=schemas.TokenOut)
def login(data: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(models.User).where(models.User.email == data.email.lower()))
    if not user or not verify_password(data.password, user.password_hash) or not user.is_active:
        raise HTTPException(401, "Email o contraseña incorrectos")
    return {"access_token": create_token(user.id), "user": user}


@app.get("/auth/me", response_model=schemas.UserOut)
def me(user: models.User = Depends(current_user)):
    return user


@app.patch("/auth/me", response_model=schemas.UserOut)
def update_my_profile(data: schemas.ProfileUpdate, user: models.User = Depends(current_user),
                      db: Session = Depends(get_db)):
    email = data.email.lower()
    changes_sensitive_data = email != user.email or data.new_password is not None
    if changes_sensitive_data and (not data.current_password or not verify_password(data.current_password, user.password_hash)):
        raise HTTPException(400, "Introduce tu contraseña actual para cambiar el correo o la contraseña")
    duplicate = db.scalar(select(models.User).where(models.User.email == email, models.User.id != user.id))
    if duplicate:
        raise HTTPException(400, "El email ya está registrado")
    user.name = data.name
    user.email = email
    if data.new_password:
        user.password_hash = hash_password(data.new_password)
    db.commit(); db.refresh(user)
    return user


@app.put("/auth/me/avatar", response_model=schemas.UserOut)
async def update_my_avatar(file: UploadFile = File(...), user: models.User = Depends(current_user),
                           db: Session = Depends(get_db)):
    content_type = (file.content_type or "").lower()
    suffix = Path(file.filename or "").suffix.lower()
    allowed = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    }
    if content_type not in allowed or suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "Selecciona una imagen JPG, PNG o WebP")
    if file.size is not None and file.size > 5 * 1024 * 1024:
        raise HTTPException(400, "La imagen no puede superar 5 MB")
    key, size = await storage.save(file)
    if size > 5 * 1024 * 1024:
        storage.delete(key)
        raise HTTPException(400, "La imagen no puede superar 5 MB")
    old_key = user.avatar_key
    user.avatar_key = key
    user.avatar_content_type = content_type
    db.commit(); db.refresh(user)
    if old_key:
        storage.delete(old_key)
    return user


@app.get("/auth/me/avatar")
def my_avatar(user: models.User = Depends(current_user)):
    if not user.avatar_key:
        raise HTTPException(404, "No tienes una imagen de perfil")
    path = storage.path(user.avatar_key)
    if not path.exists():
        raise HTTPException(404, "La imagen de perfil no está disponible")
    return FileResponse(path, media_type=user.avatar_content_type or "image/jpeg",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


BOOKING_ACTIVE_STATUSES = {"requested", "confirmed", "blocked", "unavailable"}
BOOKING_SUBJECTS = {
    "Química ESO", "Química Bachiller", "Química Universidad",
    "Física ESO", "Física Bachiller", "Matemáticas ESO", "Matemáticas Bachiller",
}


def booking_data(booking: models.Booking, viewer: models.User, include_user: bool = False) -> dict:
    is_mine = booking.user_id == viewer.id
    data = {
        "id": booking.id,
        "starts_at": booking.starts_at,
        "ends_at": booking.ends_at,
        "status": booking.status,
        "is_historical": booking.is_historical,
        "note": booking.note if (is_mine or viewer.role == "admin") else "",
        "admin_comment": booking.admin_comment if (is_mine or viewer.role == "admin") else "",
        "user_id": booking.user_id if viewer.role == "admin" else None,
        "user_name": (booking.user.name if booking.user else booking.guest_name) if include_user else None,
        "is_mine": is_mine,
    }
    return data


def booking_conflicts(starts_at: datetime, ends_at: datetime, db: Session, exclude_id: int | None = None) -> bool:
    statement = select(models.Booking).where(
        models.Booking.status.in_(BOOKING_ACTIVE_STATUSES),
        models.Booking.starts_at < ends_at,
        models.Booking.ends_at > starts_at,
    )
    if exclude_id is not None:
        statement = statement.where(models.Booking.id != exclude_id)
    return db.scalar(statement) is not None


@app.get("/public/bookings", response_model=list[schemas.PublicBookingOut])
def public_bookings(db: Session = Depends(get_db)):
    rows = list(db.scalars(select(models.Booking).where(
        models.Booking.status.in_(BOOKING_ACTIVE_STATUSES),
        models.Booking.ends_at > datetime.now(timezone.utc),
    ).order_by(models.Booking.starts_at)))
    return [{"id": row.id, "starts_at": row.starts_at, "ends_at": row.ends_at, "status": row.status} for row in rows]


@app.post("/public/trial-bookings", response_model=schemas.PublicBookingOut, status_code=201)
def request_public_trial_booking(data: schemas.PublicTrialBookingCreate, db: Session = Depends(get_db)):
    starts_at = aware(data.starts_at)
    if starts_at <= datetime.now(timezone.utc):
        raise HTTPException(400, "Solo puedes reservar franjas futuras")
    if starts_at.minute not in {0, 30} or starts_at.second or starts_at.microsecond:
        raise HTTPException(400, "La clase debe comenzar en una franja de 30 minutos")
    if data.subject not in BOOKING_SUBJECTS:
        raise HTTPException(400, "Selecciona una asignatura válida")
    ends_at = starts_at + timedelta(hours=1)
    if booking_conflicts(starts_at, ends_at, db):
        raise HTTPException(409, "Esta hora ya no está disponible")
    booking = models.Booking(guest_name=data.name.strip(), guest_email=data.email.lower(), starts_at=starts_at, ends_at=ends_at,
                             status="requested", note=f"Clase de prueba · {data.subject}")
    db.add(booking); db.commit(); db.refresh(booking)
    return {"id": booking.id, "starts_at": booking.starts_at, "ends_at": booking.ends_at, "status": booking.status}


@app.post("/public/contact-requests", response_model=schemas.ContactRequestOut, status_code=201)
def create_contact_request(data: schemas.ContactRequestCreate, db: Session = Depends(get_db)):
    request = models.ContactRequest(name=data.name.strip(), contact=data.contact.strip(), need=data.need.strip(), message=data.message.strip())
    db.add(request); db.commit(); db.refresh(request)
    notify_admin_of_contact_request(request)
    return request


@app.get("/admin/contact-requests", response_model=list[schemas.ContactRequestOut])
def contact_requests(_: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    return list(db.scalars(select(models.ContactRequest).order_by(models.ContactRequest.created_at.desc())))


@app.get("/bookings", response_model=list[schemas.BookingOut])
def bookings(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(models.Booking).order_by(models.Booking.starts_at)))
    return [booking_data(booking, user, include_user=user.role == "admin") for booking in rows]


@app.post("/bookings", response_model=schemas.BookingOut, status_code=201)
def request_booking(data: schemas.BookingCreate, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    starts_at = aware(data.starts_at)
    if starts_at <= datetime.now(timezone.utc):
        raise HTTPException(400, "Solo puedes reservar franjas futuras")
    if starts_at.minute not in {0, 30} or starts_at.second or starts_at.microsecond:
        raise HTTPException(400, "Las clases deben comenzar en una franja de 30 minutos")
    if data.subject not in BOOKING_SUBJECTS:
        raise HTTPException(400, "Selecciona una asignatura válida")
    ends_at = starts_at + timedelta(minutes=30 * data.duration_slots)
    if booking_conflicts(starts_at, ends_at, db):
        raise HTTPException(409, "Esta franja ya no está disponible")
    booking = models.Booking(user_id=user.id, starts_at=starts_at, ends_at=ends_at,
                             status="requested", note=data.subject)
    db.add(booking); db.commit(); db.refresh(booking)
    notify_admin_of_booking_request(user, booking)
    return booking_data(booking, user)


@app.post("/bookings/{booking_id}/cancel", response_model=schemas.BookingOut)
def cancel_booking(booking_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    if not booking or booking.user_id != user.id:
        raise HTTPException(404, "Reserva no encontrada")
    if booking.status not in {"requested", "confirmed"}:
        raise HTTPException(400, "Esta reserva no se puede cancelar")
    if booking.status == "confirmed" and aware(booking.starts_at) < datetime.now(timezone.utc) + timedelta(hours=24):
        raise HTTPException(400, "Para cancelar una clase con menos de 24 horas, contacta con la administradora")
    booking.status = "cancelled"
    db.commit(); db.refresh(booking)
    return booking_data(booking, user)


@app.get("/admin/booking-requests", response_model=list[schemas.BookingRequestOut])
def booking_requests(admin: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(models.Booking).where(models.Booking.status == "requested")
                           .order_by(models.Booking.starts_at)))
    return [{**booking_data(booking, admin, include_user=True),
             "user_email": booking.user.email if booking.user else None} for booking in rows]


@app.patch("/admin/bookings/{booking_id}", response_model=schemas.BookingOut)
def update_booking_status(booking_id: int, data: schemas.BookingStatusUpdate,
                          admin: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    if data.status not in {"confirmed", "declined"}:
        raise HTTPException(400, "Estado de reserva no válido")
    booking = db.get(models.Booking, booking_id)
    if not booking or booking.status != "requested":
        raise HTTPException(404, "Solicitud de reserva no encontrada")
    if data.status == "confirmed" and booking_conflicts(booking.starts_at, booking.ends_at, db, booking.id):
        raise HTTPException(409, "La franja ya está ocupada")
    booking.status = data.status
    db.commit(); db.refresh(booking)
    if booking.user:
        notify_student_of_booking_decision(booking.user, booking)
    return booking_data(booking, admin, include_user=True)


@app.post("/admin/bookings/block", response_model=schemas.BookingOut, status_code=201)
def block_booking_range(data: schemas.BookingBlockCreate, admin: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    if data.status not in {"blocked", "unavailable"}:
        raise HTTPException(400, "Elige Ocupado o No disponible")
    starts_at, ends_at = aware(data.starts_at), aware(data.ends_at)
    if ends_at <= starts_at:
        raise HTTPException(400, "La hora de fin debe ser posterior a la de inicio")
    if booking_conflicts(starts_at, ends_at, db):
        raise HTTPException(409, "El rango se solapa con una reserva o bloqueo existente")
    booking = models.Booking(starts_at=starts_at, ends_at=ends_at, status=data.status, note=data.note.strip())
    db.add(booking); db.commit(); db.refresh(booking)
    return booking_data(booking, admin, include_user=True)


@app.delete("/admin/bookings/{booking_id}/block", status_code=204)
def unblock_booking_range(booking_id: int, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    if not booking or booking.user_id is not None or booking.status not in {"blocked", "unavailable"}:
        raise HTTPException(404, "Bloqueo no encontrado")
    db.delete(booking)
    db.commit()


@app.patch("/admin/bookings/{booking_id}/block", response_model=schemas.BookingOut)
def update_blocked_range(booking_id: int, data: schemas.BookingBlockCreate,
                         admin: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    if not booking or booking.user_id is not None or booking.status not in {"blocked", "unavailable"}:
        raise HTTPException(404, "Bloqueo no encontrado")
    if data.status not in {"blocked", "unavailable"}:
        raise HTTPException(400, "Elige Ocupado o No disponible")
    starts_at, ends_at = aware(data.starts_at), aware(data.ends_at)
    if ends_at <= starts_at:
        raise HTTPException(400, "La hora de fin debe ser posterior a la de inicio")
    if booking_conflicts(starts_at, ends_at, db, booking.id):
        raise HTTPException(409, "El rango se solapa con una reserva o bloqueo existente")
    booking.starts_at, booking.ends_at = starts_at, ends_at
    booking.status, booking.note = data.status, data.note.strip()
    db.commit(); db.refresh(booking)
    return booking_data(booking, admin, include_user=True)


@app.post("/admin/classes", response_model=schemas.ClassOut, status_code=201)
def create_admin_class(data: schemas.AdminBookingCreate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    student = db.get(models.User, data.user_id)
    starts_at = aware(data.starts_at)
    if not student or student.role == "admin":
        raise HTTPException(404, "Alumno no encontrado")
    if starts_at.minute not in {0, 30} or starts_at.second or starts_at.microsecond:
        raise HTTPException(400, "La clase debe comenzar en una franja de 30 minutos")
    if data.subject not in BOOKING_SUBJECTS:
        raise HTTPException(400, "Selecciona una asignatura válida")
    ends_at = starts_at + timedelta(minutes=30 * data.duration_slots)
    if booking_conflicts(starts_at, ends_at, db):
        raise HTTPException(409, "Ese horario no está disponible")
    booking = models.Booking(user_id=student.id, starts_at=starts_at, ends_at=ends_at, status="confirmed", note=data.subject, admin_comment=data.admin_comment.strip())
    db.add(booking); db.commit(); db.refresh(booking)
    return class_data(booking)


@app.patch("/admin/classes/{booking_id}", response_model=schemas.ClassOut)
def update_admin_class(booking_id: int, data: schemas.AdminClassUpdate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    starts_at = aware(data.starts_at)
    if not booking or not booking.user:
        raise HTTPException(404, "Clase no encontrada")
    if booking.status not in {"requested", "confirmed"}:
        raise HTTPException(400, "Solo se pueden editar clases reservadas o confirmadas")
    if starts_at.minute not in {0, 30} or starts_at.second or starts_at.microsecond:
        raise HTTPException(400, "La clase debe comenzar en una franja de 30 minutos")
    if data.subject not in BOOKING_SUBJECTS:
        raise HTTPException(400, "Selecciona una asignatura válida")
    ends_at = starts_at + timedelta(minutes=30 * data.duration_slots)
    if booking_conflicts(starts_at, ends_at, db, booking.id):
        raise HTTPException(409, "Ese horario no está disponible")
    booking.starts_at, booking.ends_at, booking.note = starts_at, ends_at, data.subject
    booking.admin_comment = data.admin_comment.strip()
    db.commit(); db.refresh(booking)
    return class_data(booking)


@app.post("/admin/classes/{booking_id}/cancel", response_model=schemas.ClassOut)
def cancel_admin_class(booking_id: int, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    if not booking or not booking.user:
        raise HTTPException(404, "Clase no encontrada")
    booking.status = "cancelled"
    db.commit(); db.refresh(booking)
    return class_data(booking)


def class_data(booking: models.Booking) -> dict:
    if not booking.user:
        raise HTTPException(400, "Este bloqueo no corresponde a una clase de alumno")
    return {
        "id": booking.id,
        "starts_at": booking.starts_at,
        "ends_at": booking.ends_at,
        "status": booking.status,
        "is_historical": booking.is_historical,
        "topic": booking.note or "Clase particular",
        "admin_comment": booking.admin_comment,
        "user_id": booking.user.id,
        "user_name": booking.user.name,
        "user_email": booking.user.email,
        "materials": [
            {"id": link.material.id, "title": link.material.title, "filename": link.material.filename,
             "kind": link.material.kind}
            for link in booking.material_links
        ],
    }


@app.get("/admin/classes", response_model=list[schemas.ClassOut])
def classes(_: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(models.Booking).where(models.Booking.user_id.is_not(None))
                           .order_by(models.Booking.starts_at.desc())))
    return [class_data(booking) for booking in rows]


@app.get("/classes", response_model=list[schemas.ClassOut])
def my_classes(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(models.Booking).where(models.Booking.user_id == user.id, models.Booking.is_historical.is_(False))
                           .order_by(models.Booking.starts_at.desc())))
    return [class_data(booking) for booking in rows]


@app.patch("/admin/classes/{booking_id}/historical", response_model=schemas.ClassOut)
def set_class_historical(booking_id: int, data: schemas.ClassHistoricalUpdate,
                         _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    if not booking or not booking.user:
        raise HTTPException(404, "Clase no encontrada")
    booking.is_historical = data.is_historical
    db.commit(); db.refresh(booking)
    return class_data(booking)


@app.delete("/admin/classes/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_class(booking_id: int, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    if not booking or not booking.user:
        raise HTTPException(404, "Clase no encontrada")
    is_future = aware(booking.starts_at) > datetime.now(timezone.utc)
    if booking.status != "declined" and not is_future:
        raise HTTPException(400, "Solo puedes eliminar clases futuras o solicitudes denegadas")
    db.delete(booking)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/admin/classes/{booking_id}/attachments", response_model=schemas.ClassOut)
async def upload_class_attachment(booking_id: int, file: UploadFile = File(...), title: str = Form(""),
                                  _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    if not booking or not booking.user:
        raise HTTPException(404, "Clase no encontrada")
    if booking.status != "confirmed":
        raise HTTPException(400, "Solo puedes adjuntar archivos a clases confirmadas")
    content_type = (file.content_type or "").lower()
    suffix = Path(file.filename or "").suffix.lower()
    if content_type == "application/pdf" or suffix == ".pdf":
        kind = "pdf"; content_type = "application/pdf"
    elif content_type.startswith("video/") or suffix in {".mp4", ".webm", ".mov"}:
        kind = "video"
    else:
        raise HTTPException(400, "Solo se admiten PDF y vídeos")
    folder = db.scalar(select(models.Folder).where(
        models.Folder.name == "Material de clases", models.Folder.parent_id.is_(None)))
    if not folder:
        folder = models.Folder(name="Material de clases")
        db.add(folder); db.flush()
    key, size = await storage.save(file)
    material = models.Material(title=title.strip() or Path(file.filename or key).stem,
                               description=f"Material de la clase: {booking.note or 'Clase particular'}",
                               folder_id=folder.id, kind=kind, filename=file.filename or key,
                               storage_key=key, content_type=content_type, size_bytes=size, is_class_attachment=True)
    db.add(material); db.flush()
    db.add(models.BookingMaterial(booking_id=booking.id, material_id=material.id))
    shared_until = aware(booking.ends_at) + timedelta(days=3650)
    grant = db.scalar(select(models.AccessGrant).where(
        models.AccessGrant.user_id == booking.user_id, models.AccessGrant.material_id == material.id))
    if grant:
        grant.expires_at = max(aware(grant.expires_at), shared_until)
    else:
        db.add(models.AccessGrant(user_id=booking.user_id, material_id=material.id,
                                  starts_at=datetime.now(timezone.utc), expires_at=shared_until,
                                  can_download=False))
    db.commit(); db.refresh(booking)
    return class_data(booking)


@app.post("/admin/classes/{booking_id}/materials", response_model=schemas.ClassOut)
def attach_material_to_class(booking_id: int, data: schemas.BookingMaterialCreate,
                             _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    booking = db.get(models.Booking, booking_id)
    material = db.get(models.Material, data.material_id)
    if not booking or not booking.user or not material:
        raise HTTPException(404, "Clase o material no encontrado")
    if booking.status != "confirmed":
        raise HTTPException(400, "Solo puedes adjuntar material a clases confirmadas")
    if db.scalar(select(models.BookingMaterial).where(
        models.BookingMaterial.booking_id == booking.id,
        models.BookingMaterial.material_id == material.id,
    )):
        raise HTTPException(400, "Este material ya está adjunto a la clase")
    db.add(models.BookingMaterial(booking_id=booking.id, material_id=material.id))
    grant = db.scalar(select(models.AccessGrant).where(
        models.AccessGrant.user_id == booking.user_id,
        models.AccessGrant.material_id == material.id,
    ))
    shared_until = aware(booking.ends_at) + timedelta(days=3650)
    if grant:
        if aware(grant.expires_at) < shared_until:
            grant.expires_at = shared_until
    else:
        db.add(models.AccessGrant(user_id=booking.user_id, material_id=material.id,
                                  starts_at=datetime.now(timezone.utc), expires_at=shared_until,
                                  can_download=False))
    db.commit(); db.refresh(booking)
    return class_data(booking)


@app.get("/admin/users", response_model=list[schemas.UserOut])
def users(_: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    return list(db.scalars(select(models.User).order_by(models.User.created_at.desc())))


@app.post("/admin/users", response_model=schemas.UserOut, status_code=201)
def create_user(data: schemas.AdminUserCreate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    if data.role not in {"guest", "admin"}:
        raise HTTPException(400, "Rol no válido")
    if db.scalar(select(models.User).where(models.User.email == data.email.lower())):
        raise HTTPException(400, "El email ya está registrado")
    user = models.User(name=data.name, email=data.email.lower(), password_hash=hash_password(data.password), role=data.role)
    db.add(user); db.commit(); db.refresh(user)
    return user


@app.patch("/admin/users/{user_id}", response_model=schemas.UserOut)
def update_user(user_id: int, data: schemas.AdminUserUpdate, admin: models.User = Depends(admin_user),
                db: Session = Depends(get_db)):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    changes = data.model_dump(exclude_unset=True)
    if "role" in changes and changes["role"] not in {"guest", "admin"}:
        raise HTTPException(400, "Rol no válido")
    if "email" in changes:
        email = changes["email"].lower()
        duplicate = db.scalar(select(models.User).where(models.User.email == email, models.User.id != user_id))
        if duplicate:
            raise HTTPException(400, "El email ya está registrado")
        user.email = email
    if "name" in changes:
        user.name = changes["name"]
    if "password" in changes:
        user.password_hash = hash_password(changes["password"])
    if "is_active" in changes:
        if user.id == admin.id and not changes["is_active"]:
            raise HTTPException(400, "No puedes desactivar tu propia cuenta")
        user.is_active = changes["is_active"]
    if "role" in changes:
        if user.id == admin.id and changes["role"] != "admin":
            raise HTTPException(400, "No puedes retirar tu propio rol de administrador")
        user.role = changes["role"]
    db.commit(); db.refresh(user)
    return user


@app.delete("/admin/users/{user_id}", status_code=204)
def delete_user(user_id: int, admin: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    if user.id == admin.id:
        raise HTTPException(400, "No puedes eliminar tu propia cuenta")
    if user.role == "admin":
        raise HTTPException(400, "No se pueden eliminar cuentas de administrador")
    db.execute(delete(models.AccessGrant).where(models.AccessGrant.user_id == user_id))
    db.delete(user)
    db.commit()


def folder_path(folder: models.Folder) -> str:
    names = [folder.name]
    current = folder.parent
    while current:
        names.append(current.name)
        current = current.parent
    return " / ".join(reversed(names))


def folder_effective_color(folder: models.Folder) -> str | None:
    current: models.Folder | None = folder
    while current:
        if current.color:
            return current.color
        current = current.parent
    return None


def folder_data(folder: models.Folder) -> dict:
    return {"id": folder.id, "name": folder.name, "parent_id": folder.parent_id,
            "path": folder_path(folder), "color": folder.color,
            "effective_color": folder_effective_color(folder)}


def material_data(material: models.Material) -> dict:
    """Serialize a material including the computed path of its folder."""
    folder = material.folder
    return {
        "id": material.id,
        "title": material.title,
        "description": material.description,
        "folder": (
            folder_data(folder)
            if folder else None
        ),
        "kind": material.kind,
        "filename": material.filename,
        "content_type": material.content_type,
        "size_bytes": material.size_bytes,
        "is_favorite": False,
        "tags": [{"id": link.tag.id, "name": link.tag.name} for link in material.tag_links],
    }


def parse_tag_names(raw_tags: str) -> list[str]:
    names: list[str] = []
    for value in raw_tags.split(","):
        name = value.strip().upper()
        if not name:
            continue
        if len(name) > 80:
            raise HTTPException(400, "Cada etiqueta puede tener como máximo 80 caracteres")
        if name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    return names


def active_material_grant(material_id: int, user_id: int, now: datetime, db: Session) -> models.AccessGrant | models.TagAccessGrant | None:
    direct = db.scalar(select(models.AccessGrant).where(
        models.AccessGrant.user_id == user_id, models.AccessGrant.material_id == material_id,
        models.AccessGrant.starts_at <= now, models.AccessGrant.expires_at > now,
    ))
    tag_grant = db.scalar(select(models.TagAccessGrant).join(models.MaterialTag).where(
        models.MaterialTag.material_id == material_id,
        models.TagAccessGrant.user_id == user_id,
        models.TagAccessGrant.tag_id == models.MaterialTag.tag_id,
        models.TagAccessGrant.starts_at <= now, models.TagAccessGrant.expires_at > now,
    ).order_by(models.TagAccessGrant.expires_at.desc()))
    if direct and tag_grant:
        return direct if aware(direct.expires_at) >= aware(tag_grant.expires_at) else tag_grant
    return direct or tag_grant


@app.get("/admin/users/{user_id}/grants", response_model=list[schemas.GrantWithMaterialOut])
def user_grants(user_id: int, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    if not db.get(models.User, user_id):
        raise HTTPException(404, "Usuario no encontrado")
    rows = db.execute(
        select(models.AccessGrant, models.Material)
        .join(models.Material, models.Material.id == models.AccessGrant.material_id)
        .where(models.AccessGrant.user_id == user_id)
        .order_by(models.AccessGrant.expires_at)
    ).all()
    return [
        {
            **schemas.GrantOut.model_validate(grant).model_dump(),
            "material": {"id": material.id, "title": material.title, "filename": material.filename, "kind": material.kind},
        }
        for grant, material in rows
    ]


@app.get("/admin/folders", response_model=list[schemas.FolderOut])
def all_folders(_: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    folders = list(db.scalars(select(models.Folder).order_by(models.Folder.name)))
    return [folder_data(folder) for folder in folders]


@app.post("/admin/folders", response_model=schemas.FolderOut, status_code=201)
def create_folder(data: schemas.FolderCreate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "El nombre de la carpeta es obligatorio")
    if data.parent_id is not None and not db.get(models.Folder, data.parent_id):
        raise HTTPException(404, "La carpeta padre no existe")
    duplicate = db.scalar(select(models.Folder).where(
        models.Folder.name == name,
        models.Folder.parent_id == data.parent_id if data.parent_id is not None else models.Folder.parent_id.is_(None),
    ))
    if duplicate:
        raise HTTPException(400, "Ya existe una carpeta con ese nombre en esa ubicación")
    folder = models.Folder(name=name, parent_id=data.parent_id, color=data.color.upper() if data.color else None)
    db.add(folder); db.commit(); db.refresh(folder)
    return folder_data(folder)


@app.patch("/admin/folders/{folder_id}", response_model=schemas.FolderOut)
def update_folder(folder_id: int, data: schemas.FolderUpdate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    folder = db.get(models.Folder, folder_id)
    name = data.name.strip()
    if not folder:
        raise HTTPException(404, "Carpeta no encontrada")
    if folder.name == "Sin clasificar" and folder.parent_id is None:
        raise HTTPException(400, "La carpeta Sin clasificar no se puede modificar")
    if not name:
        raise HTTPException(400, "El nombre de la carpeta es obligatorio")
    if data.parent_id == folder.id:
        raise HTTPException(400, "Una carpeta no puede ser su propia carpeta padre")
    if data.parent_id is not None and not db.get(models.Folder, data.parent_id):
        raise HTTPException(404, "La carpeta padre no existe")
    parent = db.get(models.Folder, data.parent_id) if data.parent_id is not None else None
    if parent and len(folder_path(parent).split(" / ")) >= 3:
        raise HTTPException(400, "Solo se permiten tres niveles de carpetas")
    parent = db.get(models.Folder, data.parent_id) if data.parent_id is not None else None
    if parent and len(folder_path(parent).split(" / ")) >= 3:
        raise HTTPException(400, "Solo se permiten tres niveles de carpetas")
    while parent:
        if parent.id == folder.id:
            raise HTTPException(400, "No puedes mover una carpeta dentro de una de sus subcarpetas")
        parent = parent.parent
    duplicate = db.scalar(select(models.Folder).where(
        models.Folder.id != folder.id, models.Folder.name == name,
        models.Folder.parent_id == data.parent_id if data.parent_id is not None else models.Folder.parent_id.is_(None),
    ))
    if duplicate:
        raise HTTPException(400, "Ya existe una carpeta con ese nombre en esa ubicación")
    folder.name, folder.parent_id, folder.color = name, data.parent_id, data.color.upper() if data.color else None
    db.commit(); db.refresh(folder)
    return folder_data(folder)


@app.delete("/admin/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: int, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    folder = db.get(models.Folder, folder_id)
    if not folder:
        raise HTTPException(404, "Carpeta no encontrada")
    if folder.name == "Sin clasificar" and folder.parent_id is None:
        raise HTTPException(400, "La carpeta Sin clasificar no se puede eliminar")
    db.delete(folder)
    db.commit()


@app.get("/admin/materials", response_model=list[schemas.AdminMaterialOut])
def all_materials(user: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    materials = list(db.scalars(select(models.Material).where(models.Material.is_class_attachment.is_(False)).order_by(models.Material.created_at.desc())))
    favorite_ids = set(db.scalars(select(models.MaterialFavorite.material_id).where(models.MaterialFavorite.user_id == user.id)))
    result = []
    for material in materials:
        grants = list(db.scalars(select(models.AccessGrant).where(
            models.AccessGrant.material_id == material.id,
            models.AccessGrant.starts_at <= now,
            models.AccessGrant.expires_at > now,
        ).order_by(models.AccessGrant.expires_at)))
        result.append({
            **material_data(material),
            "is_favorite": material.id in favorite_ids,
            "grants": grants,
        })
    return result


@app.post("/materials/{material_id}/favorite", status_code=204)
def add_favorite(material_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    authorized_material(material_id, user, db)
    existing = db.scalar(select(models.MaterialFavorite).where(
        models.MaterialFavorite.user_id == user.id, models.MaterialFavorite.material_id == material_id,
    ))
    if not existing:
        db.add(models.MaterialFavorite(user_id=user.id, material_id=material_id))
        db.commit()


@app.delete("/materials/{material_id}/favorite", status_code=204)
def remove_favorite(material_id: int, user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(delete(models.MaterialFavorite).where(
        models.MaterialFavorite.user_id == user.id, models.MaterialFavorite.material_id == material_id,
    ))
    db.commit()


@app.get("/admin/tags", response_model=list[schemas.TagOut])
def all_tags(_: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    return list(db.scalars(select(models.Tag).order_by(models.Tag.name)))


@app.post("/admin/materials", response_model=schemas.MaterialOut, status_code=201)
async def upload_material(title: str = Form(...), description: str = Form(""), folder_id: int = Form(...),
                          tags: str = Form(""),
                          file: UploadFile = File(...), _: models.User = Depends(admin_user),
                          db: Session = Depends(get_db)):
    content_type = (file.content_type or "").lower()
    suffix = Path(file.filename or "").suffix.lower()
    if content_type == "application/pdf" or suffix == ".pdf":
        kind = "pdf"; content_type = "application/pdf"
    elif content_type.startswith("video/") or suffix in {".mp4", ".webm", ".mov"}:
        kind = "video"
    else:
        raise HTTPException(400, "Sólo se admiten PDF y vídeos")
    folder = db.get(models.Folder, folder_id)
    if not folder:
        raise HTTPException(404, "La carpeta seleccionada no existe")
    key, size = await storage.save(file)
    material = models.Material(title=title, description=description, folder_id=folder.id, kind=kind,
                               filename=file.filename or key, storage_key=key,
                               content_type=content_type, size_bytes=size)
    db.add(material)
    for name in parse_tag_names(tags):
        tag = db.scalar(select(models.Tag).where(models.Tag.name.ilike(name)))
        if not tag:
            tag = models.Tag(name=name)
            db.add(tag)
            db.flush()
        elif tag.name != name:
            tag.name = name
        material.tag_links.append(models.MaterialTag(tag=tag))
    db.commit(); db.refresh(material)
    return material_data(material)


@app.delete("/admin/materials/{material_id}", status_code=204)
def delete_material(material_id: int, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    material = db.get(models.Material, material_id)
    if not material:
        raise HTTPException(404, "Material no encontrado")
    storage_key = material.storage_key
    db.execute(delete(models.AccessGrant).where(models.AccessGrant.material_id == material_id))
    db.delete(material)
    db.commit()
    storage.delete(storage_key)


@app.post("/admin/grants", response_model=schemas.GrantOut)
def grant_access(data: schemas.GrantCreate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    if not db.get(models.User, data.user_id) or not db.get(models.Material, data.material_id):
        raise HTTPException(404, "Usuario o material no encontrado")
    starts = data.starts_at or datetime.now(timezone.utc)
    if aware(data.expires_at) <= aware(starts):
        raise HTTPException(400, "La fecha final debe ser posterior al inicio")
    grant = db.scalar(select(models.AccessGrant).where(
        models.AccessGrant.user_id == data.user_id,
        models.AccessGrant.material_id == data.material_id))
    if grant:
        grant.starts_at, grant.expires_at, grant.can_download = starts, data.expires_at, data.can_download
    else:
        grant = models.AccessGrant(**data.model_dump(exclude={"starts_at"}), starts_at=starts)
        db.add(grant)
    db.commit(); db.refresh(grant)
    return grant


@app.post("/admin/tag-grants", response_model=schemas.GrantOut)
def grant_tag_access(data: schemas.TagGrantCreate, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    if not db.get(models.User, data.user_id) or not db.get(models.Tag, data.tag_id):
        raise HTTPException(404, "Usuario o etiqueta no encontrados")
    starts = data.starts_at or datetime.now(timezone.utc)
    if aware(data.expires_at) <= aware(starts):
        raise HTTPException(400, "La fecha final debe ser posterior al inicio")
    grant = db.scalar(select(models.TagAccessGrant).where(
        models.TagAccessGrant.user_id == data.user_id,
        models.TagAccessGrant.tag_id == data.tag_id,
    ))
    if grant:
        grant.starts_at, grant.expires_at, grant.can_download = starts, data.expires_at, data.can_download
    else:
        grant = models.TagAccessGrant(**data.model_dump(exclude={"starts_at"}), starts_at=starts)
        db.add(grant)
    db.commit(); db.refresh(grant)
    return {"id": grant.id, "user_id": grant.user_id, "material_id": 0, "starts_at": grant.starts_at,
            "expires_at": grant.expires_at, "can_download": grant.can_download}


@app.patch("/admin/grants/{grant_id}", response_model=schemas.GrantOut)
def update_grant(grant_id: int, data: schemas.GrantUpdate, _: models.User = Depends(admin_user),
                 db: Session = Depends(get_db)):
    grant = db.get(models.AccessGrant, grant_id)
    if not grant:
        raise HTTPException(404, "Permiso no encontrado")
    if aware(data.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(400, "La nueva fecha debe ser futura")
    grant.expires_at = data.expires_at
    if data.can_download is not None:
        grant.can_download = data.can_download
    db.commit(); db.refresh(grant)
    return grant


@app.delete("/admin/grants/{grant_id}", status_code=204)
def delete_grant(grant_id: int, _: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    grant = db.get(models.AccessGrant, grant_id)
    if not grant:
        raise HTTPException(404, "Permiso no encontrado")
    db.delete(grant)
    db.commit()


@app.get("/materials", response_model=list[schemas.MaterialOut])
def my_materials(user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == "admin":
        materials = db.scalars(select(models.Material).where(models.Material.is_class_attachment.is_(False)).order_by(models.Material.created_at.desc())).all()
        favorite_ids = set(db.scalars(select(models.MaterialFavorite.material_id).where(models.MaterialFavorite.user_id == user.id)))
        return [
            {
                **material_data(material),
                "expires_at": None,
                "can_download": True,
                "is_favorite": material.id in favorite_ids,
            }
            for material in materials
        ]
    now = datetime.now(timezone.utc)
    materials = db.scalars(select(models.Material).where(
        models.Material.is_class_attachment.is_(False)).order_by(models.Material.created_at.desc())).all()
    favorite_ids = set(db.scalars(select(models.MaterialFavorite.material_id).where(models.MaterialFavorite.user_id == user.id)))
    favorite_ids = set(db.scalars(select(models.MaterialFavorite.material_id).where(models.MaterialFavorite.user_id == user.id)))
    result = []
    for material in materials:
        grant = active_material_grant(material.id, user.id, now, db)
        if grant:
            result.append({**material_data(material), "expires_at": grant.expires_at,
                           "can_download": grant.can_download, "is_favorite": material.id in favorite_ids})
    return result


def authorized_material(material_id: int, user: models.User, db: Session) -> tuple[models.Material, models.AccessGrant | None]:
    material = db.get(models.Material, material_id)
    if not material:
        raise HTTPException(404, "Material no encontrado")
    if user.role == "admin":
        return material, None
    now = datetime.now(timezone.utc)
    grant = active_material_grant(material_id, user.id, now, db)
    if not grant:
        raise HTTPException(403, "No tienes acceso activo a este material")
    return material, grant


@app.get("/materials/{material_id}/content")
def content(material_id: int, download: bool = False, range_header: str | None = Header(None, alias="Range"),
            user: models.User = Depends(current_user), db: Session = Depends(get_db)):
    material, grant = authorized_material(material_id, user, db)
    if download and user.role != "admin" and (not grant or not grant.can_download):
        raise HTTPException(403, "La descarga no está habilitada")
    path = storage.path(material.storage_key)
    if not path.exists():
        raise HTTPException(404, "El archivo no está disponible")
    disposition = "attachment" if download else "inline"
    headers = {"Content-Disposition": f'{disposition}; filename="{material.filename}"',
               "Cache-Control": "private, no-store", "Accept-Ranges": "bytes",
               "X-Content-Type-Options": "nosniff"}
    if not range_header or not material.kind == "video":
        return FileResponse(path, media_type=material.content_type, headers=headers)
    try:
        unit, values = range_header.split("=")
        start_text, end_text = values.split("-")
        if unit != "bytes":
            raise ValueError
        size = path.stat().st_size
        start = int(start_text) if start_text else 0
        end = min(int(end_text), size - 1) if end_text else min(start + 1024 * 1024 - 1, size - 1)
        if start < 0 or start > end or start >= size:
            raise ValueError
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{path.stat().st_size}"})

    def stream():
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
    return StreamingResponse(stream(), status_code=206, media_type=material.content_type, headers=headers)
