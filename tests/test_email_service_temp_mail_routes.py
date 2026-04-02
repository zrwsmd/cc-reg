import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.web.routes import email as email_routes


def _build_test_db(db_name: str):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / db_name
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_create_temp_mail_service_normalizes_base_url(monkeypatch):
    manager = _build_test_db("temp_mail_routes_create.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    request = email_routes.EmailServiceCreate(
        service_type="temp_mail",
        name="rw-tempmail1",
        config={
            "base_url": " alison.rwsmd.eu.cc/ ",
            "admin_password": "admin-secret",
            "domain": "@rwsmd.eu.cc ",
            "enable_prefix": True,
        },
    )

    response = asyncio.run(email_routes.create_email_service(request))

    assert response.config["base_url"] == "https://alison.rwsmd.eu.cc"
    assert response.config["domain"] == "rwsmd.eu.cc"

    with manager.session_scope() as session:
        service = session.query(EmailService).filter(EmailService.name == "rw-tempmail1").first()
        assert service is not None
        assert service.config["base_url"] == "https://alison.rwsmd.eu.cc"
        assert service.config["domain"] == "rwsmd.eu.cc"


def test_update_temp_mail_service_keeps_existing_secret_and_false_flags(monkeypatch):
    manager = _build_test_db("temp_mail_routes_update.db")

    with manager.session_scope() as session:
        service = EmailService(
            service_type="temp_mail",
            name="rw-tempmail1",
            config={
                "base_url": "https://old.example.com",
                "admin_password": "admin-secret",
                "domain": "rwsmd.eu.cc",
                "enable_prefix": False,
            },
            enabled=True,
            priority=0,
        )
        session.add(service)
        session.flush()
        service_id = service.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    request = email_routes.EmailServiceUpdate(
        config={
            "base_url": " alison.rwsmd.eu.cc/ ",
            "admin_password": "   ",
        }
    )

    response = asyncio.run(email_routes.update_email_service(service_id, request))

    assert response.config["base_url"] == "https://alison.rwsmd.eu.cc"
    assert response.config["enable_prefix"] is False
    assert response.config["has_admin_password"] is True

    with manager.session_scope() as session:
        service = session.query(EmailService).filter(EmailService.id == service_id).first()
        assert service is not None
        assert service.config["base_url"] == "https://alison.rwsmd.eu.cc"
        assert service.config["admin_password"] == "admin-secret"
        assert service.config["enable_prefix"] is False
