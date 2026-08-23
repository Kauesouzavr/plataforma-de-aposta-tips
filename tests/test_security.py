import os

os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["MP_ACCESS_TOKEN"] = "TEST-TOKEN"
os.environ["ADMIN_PASSWORD"] = "admin-test-password"
os.environ["PALPITES_PASSWORD"] = "painel-test-password"
os.environ["BREVO_API_KEY"] = "brevo-test"
os.environ["BREVO_SENDER_EMAIL"] = "noreply@example.com"
os.environ["BASE_URL"] = "http://localhost:5000"
os.environ["DATABASE_URL"] = "postgresql://user:pass@localhost:5432/testdb"
os.environ["SKIP_INIT_DB"] = "1"

import pytest
from unittest.mock import patch

from app import app, email_valido, link_https_seguro


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as client:
        yield client


def test_email_valido():
    assert email_valido("user@example.com")
    assert not email_valido("")
    assert not email_valido("sem-arroba")
    assert not email_valido("a" * 200 + "@x.com")


def test_link_https_seguro_bloqueia_xss():
    assert link_https_seguro("https://www.bet365.com/x") == "https://www.bet365.com/x"
    assert link_https_seguro("javascript:alert(1)") is None
    assert link_https_seguro("http://evil.example/phish") is None
    assert link_https_seguro("data:text/html,hi") is None


@patch("app.marcar_pago")
@patch("app.resolver_email_pago")
def test_sucesso_nao_libera_acesso_so_com_query_string(mock_resolver, mock_marcar, client):
    mock_resolver.return_value = None
    resposta = client.get(
        "/sucesso?status=approved&external_reference=hacker@example.com",
        follow_redirects=False,
    )
    mock_marcar.assert_not_called()
    mock_resolver.assert_not_called()
    assert resposta.status_code == 200
    assert resposta.headers.get("Location") not in {"/curso", "http://localhost/curso"}


@patch("app.registrar_dispositivo")
@patch("app.marcar_pago")
@patch("app.resolver_email_pago", return_value="pago@example.com")
def test_sucesso_libera_quando_api_confirma(mock_resolver, mock_marcar, mock_device, client):
    resposta = client.get("/sucesso?payment_id=12345", follow_redirects=False)
    mock_resolver.assert_called_once_with("payment", "12345")
    mock_marcar.assert_called_once_with("pago@example.com")
    assert resposta.status_code == 302
    assert resposta.headers["Location"].endswith("/curso")