from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, schemas
from .config import settings
from .database import Base, SessionLocal, engine, get_db
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    seed_admin()
    yield


app = FastAPI(title="Chemistry Academy API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok"}


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


@app.get("/admin/materials", response_model=list[schemas.AdminMaterialOut])
def all_materials(_: models.User = Depends(admin_user), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    materials = list(db.scalars(select(models.Material).order_by(models.Material.created_at.desc())))
    result = []
    for material in materials:
        grants = list(db.scalars(select(models.AccessGrant).where(
            models.AccessGrant.material_id == material.id,
            models.AccessGrant.starts_at <= now,
            models.AccessGrant.expires_at > now,
        ).order_by(models.AccessGrant.expires_at)))
        result.append({
            **schemas.MaterialOut.model_validate(material).model_dump(),
            "grants": grants,
        })
    return result


@app.post("/admin/materials", response_model=schemas.MaterialOut, status_code=201)
async def upload_material(title: str = Form(...), description: str = Form(""),
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
    key, size = await storage.save(file)
    material = models.Material(title=title, description=description, kind=kind,
                               filename=file.filename or key, storage_key=key,
                               content_type=content_type, size_bytes=size)
    db.add(material); db.commit(); db.refresh(material)
    return material


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
        materials = db.scalars(select(models.Material).order_by(models.Material.created_at.desc())).all()
        return [
            {
                **schemas.MaterialOut.model_validate(material).model_dump(),
                "expires_at": None,
                "can_download": True,
            }
            for material in materials
        ]
    now = datetime.now(timezone.utc)
    rows = db.execute(select(models.Material, models.AccessGrant).join(models.AccessGrant).where(
        models.AccessGrant.user_id == user.id,
        models.AccessGrant.starts_at <= now,
        models.AccessGrant.expires_at > now).order_by(models.Material.created_at.desc())).all()
    return [{**schemas.MaterialOut.model_validate(material).model_dump(),
             "expires_at": grant.expires_at, "can_download": grant.can_download}
            for material, grant in rows]


def authorized_material(material_id: int, user: models.User, db: Session) -> tuple[models.Material, models.AccessGrant | None]:
    material = db.get(models.Material, material_id)
    if not material:
        raise HTTPException(404, "Material no encontrado")
    if user.role == "admin":
        return material, None
    now = datetime.now(timezone.utc)
    grant = db.scalar(select(models.AccessGrant).where(
        models.AccessGrant.user_id == user.id, models.AccessGrant.material_id == material_id,
        models.AccessGrant.starts_at <= now, models.AccessGrant.expires_at > now))
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
