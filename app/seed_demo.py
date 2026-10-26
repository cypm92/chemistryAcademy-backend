"""Datos de demostración reproducibles para el entorno local de Be Ciencia.

Se ejecuta solamente de forma explícita con ``python -m app.seed_demo``.
No forma parte del arranque normal de la aplicación ni se usa en producción.
"""

from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select

from . import models
from .database import SessionLocal
from .security import hash_password
from .storage import storage


DEMO_PASSWORD = "AlumnoDemo123!"
DEMO_ADMIN_PASSWORD = "DemoAdmin123!"


def demo_pdf(title: str) -> bytes:
    """Genera un PDF mínimo y válido para que el visor de materiales sea usable."""
    safe_title = title.encode("ascii", "replace").decode("ascii")
    stream = f"BT /F1 22 Tf 72 720 Td ({safe_title}) Tj 0 -34 Td /F1 12 Tf (Material de demostracion para el entorno local de Be Ciencia.) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode())} >>\nstream\n{stream}\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, content in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n{content}\nendobj\n".encode())
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def next_slot(days_from_now: int, hour: int, minute: int = 0) -> datetime:
    now = datetime.now(timezone.utc)
    result = (now + timedelta(days=days_from_now)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return result if result > now else result + timedelta(days=7)


def main() -> None:
    with SessionLocal() as db:
        def user(name: str, email: str, role: str = "guest", active: bool = True) -> models.User:
            row = db.scalar(select(models.User).where(models.User.email == email))
            if not row:
                row = models.User(name=name, email=email, password_hash=hash_password(DEMO_ADMIN_PASSWORD if role == "admin" else DEMO_PASSWORD))
                db.add(row)
            row.name, row.role, row.is_active = name, role, active
            row.password_hash = hash_password(DEMO_ADMIN_PASSWORD if role == "admin" else DEMO_PASSWORD)
            return row

        alba = user("Alba Martín", "alba@demo.beciencia.academy")
        diego = user("Diego Ramos", "diego@demo.beciencia.academy")
        noelia = user("Noelia Suárez", "noelia@demo.beciencia.academy")
        inactive = user("Hugo Díaz", "hugo@demo.beciencia.academy", active=False)
        demo_admin = user("Marta Demo", "marta@demo.beciencia.academy", role="admin")

        def folder(name: str, parent: models.Folder | None = None, color: str | None = None) -> models.Folder:
            statement = select(models.Folder).where(models.Folder.name == name)
            statement = statement.where(models.Folder.parent_id == parent.id) if parent else statement.where(models.Folder.parent_id.is_(None))
            row = db.scalar(statement)
            if not row:
                row = models.Folder(name=name, parent_id=parent.id if parent else None)
                db.add(row)
            row.color = color
            db.flush()
            return row

        chemistry = folder("Química", color="#5B987D")
        solutions = folder("Disoluciones", chemistry)
        exercises = folder("Ejercicios resueltos", solutions)
        exercise_one = folder("Ejercicio 1", exercises)
        review_problems = folder("Problemas de repaso", exercise_one)
        folder("Ejercicio 2", exercises)
        folder("Ejercicio 3", exercises)
        equilibrium = folder("Equilibrio químico", chemistry)
        organic = folder("Química orgánica", color="#AAA1C9")
        nomenclature = folder("Nomenclatura", organic)
        physics = folder("Física", color="#E8A161")
        kinematics = folder("Cinemática", physics)
        maths = folder("Matemáticas", color="#8EAFc7")
        derivatives = folder("Derivadas", maths)

        tags_by_name: dict[str, models.Tag] = {}
        for name in ("BACHILLERATO", "UNIVERSIDAD", "PAU", "ORGÁNICA", "EQUILIBRIO", "FORMULACIÓN"):
            row = db.scalar(select(models.Tag).where(models.Tag.name == name))
            if not row:
                row = models.Tag(name=name)
                db.add(row)
            tags_by_name[name] = row
        db.flush()

        material_specs = [
            ("Disoluciones: guía de concentración", "Ejercicios guiados de molaridad, porcentajes y diluciones.", solutions, ("BACHILLERATO", "PAU")),
            ("Equilibrio químico paso a paso", "Resumen con ejercicios resueltos y errores habituales.", equilibrium, ("BACHILLERATO", "EQUILIBRIO")),
            ("Nomenclatura orgánica esencial", "Hoja de trabajo para repasar grupos funcionales.", nomenclature, ("BACHILLERATO", "ORGÁNICA")),
            ("Cinemática: problemas tipo PAU", "Colección de problemas de movimiento rectilíneo y circular.", kinematics, ("BACHILLERATO", "PAU")),
            ("Derivadas y optimización", "Problemas seleccionados para primer curso universitario.", derivatives, ("UNIVERSIDAD",)),
            ("Problemas de repaso: disoluciones", "Material de prueba situado en el quinto nivel de carpetas.", review_problems, ("BACHILLERATO", "PAU")),
        ]
        materials: dict[str, models.Material] = {}
        for index, (title, description, parent, tag_names) in enumerate(material_specs, start=1):
            key = f"demo-material-{index}.pdf"
            path = storage.path(key)
            path.write_bytes(demo_pdf(title))
            row = db.scalar(select(models.Material).where(models.Material.storage_key == key))
            if not row:
                row = models.Material(title=title, description=description, folder_id=parent.id, kind="pdf", filename=key,
                                      storage_key=key, content_type="application/pdf", size_bytes=path.stat().st_size)
                db.add(row)
                db.flush()
            row.title, row.description, row.folder_id = title, description, parent.id
            row.filename, row.content_type, row.size_bytes = key, "application/pdf", path.stat().st_size
            row.is_class_attachment = False
            db.execute(delete(models.MaterialTag).where(models.MaterialTag.material_id == row.id))
            for tag in tag_names:
                db.add(models.MaterialTag(material_id=row.id, tag_id=tags_by_name[tag].id))
            materials[title] = row
        db.flush()

        expiry = datetime.now(timezone.utc) + timedelta(days=30)
        demo_student_ids = [alba.id, diego.id, noelia.id, inactive.id]
        # Reinicia solamente los permisos y favoritos de los usuarios demo para
        # que ejecutar la semilla varias veces mantenga exactamente el mismo estado.
        db.execute(delete(models.AccessGrant).where(models.AccessGrant.user_id.in_(demo_student_ids)))
        db.execute(delete(models.TagAccessGrant).where(models.TagAccessGrant.user_id.in_(demo_student_ids)))
        db.execute(delete(models.MaterialFavorite).where(models.MaterialFavorite.user_id.in_(demo_student_ids)))
        db.flush()

        def direct_grant(student: models.User, material: models.Material, can_download: bool = False) -> None:
            row = db.scalar(select(models.AccessGrant).where(models.AccessGrant.user_id == student.id, models.AccessGrant.material_id == material.id))
            if not row:
                row = models.AccessGrant(user_id=student.id, material_id=material.id, starts_at=datetime.now(timezone.utc), expires_at=expiry)
                db.add(row)
            row.expires_at, row.can_download = expiry, can_download

        direct_grant(alba, materials["Equilibrio químico paso a paso"], True)
        direct_grant(diego, materials["Nomenclatura orgánica esencial"])
        tag_grant = db.scalar(select(models.TagAccessGrant).where(models.TagAccessGrant.user_id == noelia.id, models.TagAccessGrant.tag_id == tags_by_name["PAU"].id))
        if not tag_grant:
            tag_grant = models.TagAccessGrant(user_id=noelia.id, tag_id=tags_by_name["PAU"].id, starts_at=datetime.now(timezone.utc), expires_at=expiry)
            db.add(tag_grant)
        tag_grant.expires_at, tag_grant.can_download = expiry, False

        db.add(models.MaterialFavorite(user_id=alba.id, material_id=materials["Equilibrio químico paso a paso"].id))

        for booking in list(db.scalars(select(models.Booking).where(models.Booking.note.like("Demo ·%")))):
            db.delete(booking)
        for request in list(db.scalars(select(models.ContactRequest).where(
            models.ContactRequest.name.in_(["Elena García", "Sergio López"]),
        ))):
            db.delete(request)
        db.flush()

        confirmed = models.Booking(user_id=alba.id, starts_at=next_slot(2, 17), ends_at=next_slot(2, 18), status="confirmed",
                                   note="Demo · Química Bachiller", admin_comment="Repasar disoluciones antes de la clase.")
        requested = models.Booking(user_id=diego.id, starts_at=next_slot(3, 18), ends_at=next_slot(3, 19), status="requested",
                                   note="Demo · Química Universidad")
        blocked = models.Booking(starts_at=next_slot(4, 16), ends_at=next_slot(4, 18), status="blocked", note="Demo · Horario no disponible")
        guest = models.Booking(guest_name="Lucía Interesada", guest_email="lucia@demo.beciencia.academy", starts_at=next_slot(5, 17),
                               ends_at=next_slot(5, 18), status="requested", note="Demo · Clase de prueba · Química Bachiller")
        historical_start = (datetime.now(timezone.utc) - timedelta(days=5)).replace(hour=17, minute=0, second=0, microsecond=0)
        historical = models.Booking(user_id=noelia.id, starts_at=historical_start, ends_at=historical_start + timedelta(hours=1), status="confirmed",
                                    is_historical=True, note="Demo · Matemáticas Bachiller", admin_comment="Clase completada: derivadas y optimización.")
        db.add_all([confirmed, requested, blocked, guest, historical])
        db.flush()
        attachment_key = "demo-material-clase.pdf"
        attachment_path = storage.path(attachment_key)
        attachment_path.write_bytes(demo_pdf("Material de clase: disoluciones"))
        attachment = db.scalar(select(models.Material).where(models.Material.storage_key == attachment_key))
        if not attachment:
            attachment = models.Material(title="Material de clase · Disoluciones", description="Adjunto de una clase demo.",
                                         folder_id=solutions.id, kind="pdf", filename=attachment_key, storage_key=attachment_key,
                                         content_type="application/pdf", size_bytes=attachment_path.stat().st_size, is_class_attachment=True)
            db.add(attachment)
            db.flush()
        attachment.is_class_attachment = True
        db.add(models.BookingMaterial(booking_id=confirmed.id, material_id=attachment.id))

        db.add_all([
            models.ContactRequest(name="Elena García", contact="elena@demo.beciencia.academy", need="Clases particulares", message="Necesito reforzar equilibrio para la PAU."),
            models.ContactRequest(name="Sergio López", contact="600 123 456", need="Clases en grupo", message="¿Hay grupo de Química Orgánica este trimestre?"),
        ])
        db.commit()

    print("Datos demo creados: 4 alumnos, 1 administradora, 14 carpetas, 5 materiales, accesos, reservas y solicitudes.")
    print(f"Alumnos demo: alba@demo.beciencia.academy / {DEMO_PASSWORD}")
    print(f"Admin demo: marta@demo.beciencia.academy / {DEMO_ADMIN_PASSWORD}")


if __name__ == "__main__":
    main()
