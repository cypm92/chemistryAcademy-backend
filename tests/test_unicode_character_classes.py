"""Pruebas automáticas sobre clases de caracteres unicode en nombres de materiales.

Cubre:
  * ASCII
  * Acentos compuestos (un solo punto de código, p. ej. U+00ED para í)
  * Acentos combinados (forma descompuesta, p. ej. i + U+0301)
  * Acento n tilde: tanto compuesto (U+00F1) como combinado (n + U+0303)
  * Comillas: dobles (") y simples (') dentro del nombre del archivo

No modifica el viewer, ni migra archivos ni despliega.
"""

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from ephemeral_postgres import postgres_test_db

from app import models
from app.database import Base, get_db
from app.main import app
from app.security import create_token
from app.storage import storage


@pytest.fixture
def material_client(tmp_path, monkeypatch, postgres_test_db):
    """Cliente HTTP con base de datos PostgreSQL efímera y almacenamiento temporal."""
    engine = postgres_test_db.engine
    Base.metadata.create_all(engine)
    sessions = postgres_test_db.session_factory

    def local_db():
        with sessions() as db:
            yield db

    with sessions() as db:
        admin = models.User(name="Admin", email="admin@example.com", password_hash="x", role="admin")
        folder = models.Folder(name="Materials")
        db.add_all([admin, folder])
        db.commit()
        admin_id, folder_id = admin.id, folder.id

    monkeypatch.setattr(storage, "root", tmp_path.resolve())
    app.dependency_overrides[get_db] = local_db
    client = TestClient(app)
    try:
        yield client, {"Authorization": f"Bearer {create_token(admin_id)}"}, folder_id
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def fetch_content(client, material_id, auth=None, download=False):
    headers = dict(auth) if auth else None
    params = {"download": str(download).lower()}
    return client.get(f"/materials/{material_id}/content", headers=headers, params=params)


def assert_material_served(material, filename):
    assert material["filename"] == filename, (material["filename"], filename)


@pytest.fixture
def client(material_client):
    return material_client[0]


@pytest.fixture
def auth(material_client):
    return material_client[1]


@pytest.fixture
def upload(material_client):
    """Sube un material y devuelve el JSON de la respuesta."""

    def _upload(filename, content=b"0123456789", mime="application/pdf"):
        client, auth, folder_id = material_client
        response = client.post(
            "/admin/materials",
            headers=auth,
            data={"title": "Test", "folder_id": str(folder_id)},
            files={"file": (filename, content, mime)},
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _upload


def assert_hostname_disposition_safe(disposition, expected_filename, disposition_type="attachment"):
    """Revisa que content-disposition codifica los nombres unicode sin comillas crudas."""
    prefix = f"{disposition_type}; filename*=UTF-8''"
    assert disposition.startswith(prefix), disposition
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    # La codificación URI no deja comillas dobles ni simples crudas.
    assert '"' not in encoded, disposition
    assert "'" not in encoded, disposition
    assert unquote(encoded) == expected_filename, (disposition, expected_filename)


# Clases de caracteres cubiertas.
ASCII_NAMES = [
    "a.txt",
    "material-001.pdf",
    "QUIZ_2024.txt",
    "zebra99",
]

COMPOSED_ACCENT_NAMES = [
    "química.pdf",      # í = U+00ED
    "año.pdf",          # ñ = U+00F1
    "niño.pdf",         # í
    "sábado.pdf",       # á
    "aspa.pdf",         # á
]

COMBINING_ACCENT_NAMES = [
    "qui\u0301mica.pdf",  # é = e + U+0301
    "a\u0301no.pdf",      # á = a + U+0301
    "ni\u0301o.pdf",      # í = i + U+0301
    "si\u0301laba.pdf",   # í = i + U+0301
    "o\u0301.txt",        # ó = o + U+0301
]

N_TILDE_NAMES = [
    "señal.pdf",        # ñ compuesto = U+00F1
    "sen\u0303al.pdf",   # ñ combinado = n + U+0303
    "an\u0303o.pdf",      # ñ combinado = n + U+0303
]

QUOTE_NAMES = [
    'notas "uno".pdf',           # comilla doble
    "notas 'uno'.pdf",           # comilla simple
    'archivo "con" "espacios".pdf',
    'apellidos "pérez" y "garcía".pdf',  # una combinada con comillas
]


@pytest.mark.parametrize("filename", ASCII_NAMES, ids=lambda f: f"ascii/{f}")
def test_ascii_names_upload_serve_and_disposition(upload, client, auth, filename):
    material = upload(filename)
    assert_material_served(material, filename)
    response = fetch_content(client, material["id"], auth=auth, download=False)
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert_hostname_disposition_safe(response.headers["content-disposition"], filename, "inline")


@pytest.mark.parametrize("filename", COMPOSED_ACCENT_NAMES, ids=lambda f: f"composed/{f}")
def test_composed_accent_names_round_trip(upload, client, auth, filename):
    material = upload(filename)
    assert_material_served(material, filename)
    response = fetch_content(client, material["id"], auth=auth, download=False)
    assert response.status_code == 200
    assert response.content == b"0123456789"


@pytest.mark.parametrize("filename", COMBINING_ACCENT_NAMES, ids=lambda f: f"combining/{f}")
def test_combining_accent_names_round_trip(upload, client, auth, filename):
    material = upload(filename)
    assert_material_served(material, filename)
    response = fetch_content(client, material["id"], auth=auth, download=False)
    assert response.status_code == 200
    assert response.content == b"0123456789"


@pytest.mark.parametrize("filename", N_TILDE_NAMES, ids=lambda f: f"ntilde/{f}")
def test_n_tilde_names_round_trip(upload, client, auth, filename):
    material = upload(filename)
    assert_material_served(material, filename)
    response = fetch_content(client, material["id"], auth=auth, download=False)
    assert response.status_code == 200
    assert response.content == b"0123456789"


@pytest.mark.parametrize("filename", QUOTE_NAMES, ids=lambda f: f"quotes/{f}")
def test_quote_names_safely_encoded_in_disposition(upload, client, auth, filename):
    material = upload(filename)
    assert material["filename"] == filename.replace('"', '%22')
    response = fetch_content(client, material["id"], auth=auth, download=False)
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert_hostname_disposition_safe(response.headers["content-disposition"], material["filename"], "inline")
