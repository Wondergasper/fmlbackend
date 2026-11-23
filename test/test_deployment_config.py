from pathlib import Path


def test_render_procfile_uses_asgi_worker():
    procfile = Path(__file__).resolve().parents[1] / "Procfile"

    assert procfile.exists(), "Procfile is required for Render deployment"

    content = procfile.read_text(encoding="utf-8")
    assert "uvicorn" in content.lower(), "Procfile must use uvicorn for the FastAPI app"


def test_storefront_origins_survive_legacy_environment_configuration():
    from config import Settings
    settings = Settings(_env_file=None, cors_origins=' https://legacy.example/ , ,http://localhost:5173 ')
    assert settings.allowed_cors_origins == [
        'https://farmersmarketltd.com', 'https://www.farmersmarketltd.com',
        'https://legacy.example', 'http://localhost:5173',
    ]


def test_storefront_login_preflight_and_untrusted_origins():
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    headers = {'Access-Control-Request-Method':'POST',
               'Access-Control-Request-Headers':'content-type,authorization,idempotency-key'}
    for origin in ('https://farmersmarketltd.com', 'https://www.farmersmarketltd.com'):
        response = client.options('/auth/login', headers={**headers,'Origin':origin})
        assert response.status_code == 200
        assert response.headers['access-control-allow-origin'] == origin
        assert response.headers['access-control-allow-credentials'] == 'true'
    for origin in ('https://farmersmarketltd.com.attacker.example', 'https://attacker.example'):
        assert client.options('/auth/login', headers={**headers,'Origin':origin}).status_code == 400


def test_catalog_connection_error_returns_503_with_cors(monkeypatch):
    import httpx
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from main import app
    from routers import products
    query = MagicMock()
    query.order.return_value.execute.side_effect = httpx.ConnectError('private-db-host is unavailable')
    monkeypatch.setattr(products, 'catalog_query', lambda: query)
    response = TestClient(app).get('/products/', headers={'Origin':'https://farmersmarketltd.com'})
    assert response.status_code == 503
    assert response.headers['access-control-allow-origin'] == 'https://farmersmarketltd.com'
    assert response.headers['retry-after'] == '5'
    assert 'private-db-host' not in response.text


def test_login_connection_error_is_not_reported_as_bad_credentials(monkeypatch):
    import httpx
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from main import app
    from routers import auth
    db = MagicMock()
    db.auth.sign_in_with_password.side_effect = httpx.ConnectError('private-db-host is unavailable')
    monkeypatch.setattr(auth, 'supabase', db)
    response = TestClient(app).post('/auth/login', json={'email':'buyer@example.com','password':'password'})
    assert response.status_code == 503
    assert 'private-db-host' not in response.text


def test_readiness_checks_database_and_head_is_supported(monkeypatch):
    import httpx
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    import main
    db = MagicMock()
    monkeypatch.setattr(main, 'supabase_admin', db)
    client = TestClient(main.app)
    assert client.head('/').status_code == 200
    assert client.get('/health/ready').json() == {'status':'ready','database':'available'}
    db.table.return_value.select.return_value.limit.return_value.execute.side_effect = httpx.ConnectError('private-db-host')
    response = client.get('/health/ready')
    assert response.status_code == 503
    assert response.json() == {'status':'not_ready','database':'unavailable'}


def test_auth_session_connection_failure_returns_503(monkeypatch):
    import asyncio
    import httpx
    import pytest
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from middleware import auth
    db = MagicMock()
    db.auth.get_user.side_effect = httpx.ConnectError('private-db-host')
    monkeypatch.setattr(auth, 'supabase', db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.get_current_user(HTTPAuthorizationCredentials(scheme='Bearer', credentials='token')))
    assert exc.value.status_code == 503


def test_supabase_wrapped_auth_connection_errors_return_503(monkeypatch):
    import asyncio
    from unittest.mock import MagicMock
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from fastapi.testclient import TestClient
    from gotrue.errors import AuthRetryableError
    import pytest
    from main import app
    from routers import auth
    from middleware import auth as auth_middleware
    db = MagicMock()
    db.auth.sign_in_with_password.side_effect = AuthRetryableError('private-db-host', 0)
    db.auth.sign_up.side_effect = AuthRetryableError('private-db-host', 0)
    db.auth.get_user.side_effect = AuthRetryableError('private-db-host', 0)
    monkeypatch.setattr(auth, 'supabase', db)
    monkeypatch.setattr(auth_middleware, 'supabase', db)
    client = TestClient(app)
    assert client.post('/auth/login', json={'email':'buyer@example.com','password':'password'}).status_code == 503
    assert client.post('/auth/register', json={'email':'buyer@example.com','password':'password','full_name':'Buyer'}).status_code == 503
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_middleware.get_current_user(HTTPAuthorizationCredentials(scheme='Bearer', credentials='token')))
    assert exc.value.status_code == 503
