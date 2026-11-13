from ephemeral_postgres import postgres_test_db
from fastapi.testclient import TestClient


def test_real_app_exposes_disabled_status_and_protects_routes(monkeypatch):
    from app.main import app
    from app.security import admin_user
    for name in ('MICROSOFT_TENANT_ID', 'MICROSOFT_CLIENT_ID', 'MICROSOFT_CLIENT_SECRET', 'MICROSOFT_REDIRECT_URI', 'MICROSOFT_FERNET_KEY'):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(app)
    for method, path in [('get', '/admin/integrations/microsoft'), ('post', '/admin/integrations/microsoft/start'), ('post', '/admin/integrations/microsoft/disconnect')]:
        assert getattr(client, method)(path).status_code == 401
    app.dependency_overrides[admin_user] = lambda: object()
    try:
        response = client.get('/admin/integrations/microsoft')
        assert response.json() == {'configured': False, 'connected': False, 'account_label': None, 'needs_reconnect': False}
        assert client.post('/admin/integrations/microsoft/start').status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_class_response_exposes_disabled_teams_state(monkeypatch):
    from datetime import datetime, timezone
    from app.main import class_data
    from app.models import Booking
    from app.schemas import ClassOut
    for name in ("MICROSOFT_TENANT_ID", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET", "MICROSOFT_REDIRECT_URI", "MICROSOFT_FERNET_KEY"):
        monkeypatch.delenv(name, raising=False)
    booking = Booking(id=1, guest_name="Test", guest_email="test@example.com", starts_at=datetime.now(timezone.utc), ends_at=datetime.now(timezone.utc), status="confirmed", is_historical=False, admin_comment="")
    assert ClassOut.model_validate(class_data(booking)).model_dump()["teams_status"] == "not_configured"


def test_full_app_oauth_flow_and_permissions(monkeypatch, postgres_test_db):
    from urllib.parse import parse_qs, urlparse
    import httpx
    from cryptography.fernet import Fernet
    from sqlalchemy import inspect, text
    from sqlalchemy.orm import sessionmaker
    from app.database import Base, get_db
    from app.main import app
    from app.models import User
    from app.microsoft_auth_models import MicrosoftCredential
    from app.microsoft_oauth_routes import get_http_transport, get_session_factory
    from app.security import create_token

    for key, value in {"MICROSOFT_TENANT_ID": "11111111-1111-4111-8111-111111111111", "MICROSOFT_CLIENT_ID": "22222222-2222-4222-8222-222222222222", "MICROSOFT_CLIENT_SECRET": "fake", "MICROSOFT_REDIRECT_URI": "https://example.test/api/integrations/microsoft/callback", "MICROSOFT_FERNET_KEY": Fernet.generate_key().decode()}.items():
        monkeypatch.setenv(key, value)
    engine = postgres_test_db.engine
    # Simulate an existing installation, not a fresh metadata-only database.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO legacy_marker VALUES (7)"))
    Base.metadata.create_all(engine)
    assert {"microsoft_oauth_states", "microsoft_credentials", "microsoft_oauth_generation", "teams_sync_jobs"} <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT id FROM legacy_marker")) == 7
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        admin = User(name="Admin", email="admin@example.com", password_hash="x", role="admin", is_active=True)
        student = User(name="Student", email="student@example.com", password_hash="x", role="student", is_active=True)
        db.add_all([admin, student]); db.flush()
        admin_id, student_id = admin.id, student.id
    def database():
        with factory() as db:
            yield db
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "fake-access", "refresh_token": "fake-refresh"})
        assert request.url.host == "graph.microsoft.com"
        return httpx.Response(200, json={"id": "33333333-3333-4333-8333-333333333333", "displayName": "Test"})
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_session_factory] = lambda: factory
    app.dependency_overrides[get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        browser = TestClient(app, base_url="https://example.test", follow_redirects=False)
        admin_header = {"Authorization": "Bearer " + create_token(admin_id)}
        student_header = {"Authorization": "Bearer " + create_token(student_id)}
        for method, path in [("get", "/admin/integrations/microsoft"), ("post", "/admin/integrations/microsoft/start"), ("post", "/admin/integrations/microsoft/disconnect")]:
            assert getattr(browser, method)(path, headers=student_header).status_code == 403
        started = browser.post("/admin/integrations/microsoft/start", headers=admin_header)
        assert started.status_code == 200
        cookie = started.headers["set-cookie"]
        assert "Path=/api/integrations/microsoft/callback" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
        nonce = browser.cookies.get("microsoft_oauth_nonce")
        # nginx strips /api before forwarding; the browser sends its public-path cookie.
        result = browser.get("/integrations/microsoft/callback", params={"state": state, "code": "fake-code"}, headers={"Cookie": "microsoft_oauth_nonce=" + nonce})
        assert result.headers["location"] == "/admin?microsoft=connected"
        assert len(calls) == 2
        with factory() as db:
            assert "fake-refresh" not in db.get(MicrosoftCredential, 1).refresh_token_ciphertext
        assert browser.get("/admin/integrations/microsoft", headers=admin_header).json()["connected"] is True
        assert browser.post("/admin/integrations/microsoft/disconnect", headers=admin_header).status_code == 200
        assert browser.get("/admin/integrations/microsoft", headers=admin_header).json()["connected"] is False
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
