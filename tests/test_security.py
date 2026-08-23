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
from datetime import datetime, timedelta
from unittest.mock import patch

from app import app, email_valido, link_https_seguro, detectar_modo_mp, link_checkout_mp, vip_ainda_valido


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


def test_detectar_modo_mp_pelo_token():
    assert detectar_modo_mp("TEST-abc", "") == "test"
    assert detectar_modo_mp("APP_USR-abc", "") == "production"


def test_detectar_modo_mp_rejeita_mistura():
    with pytest.raises(RuntimeError):
        detectar_modo_mp("TEST-abc", "production")
    with pytest.raises(RuntimeError):
        detectar_modo_mp("APP_USR-abc", "test")


def test_link_checkout_nao_usa_sandbox_em_producao():
    resp = {
        "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=real",
        "sandbox_init_point": "https://sandbox.mercadopago.com.br/checkout/v1/redirect?pref_id=fake",
    }
    assert "sandbox" not in link_checkout_mp(resp, "production")
    assert "sandbox" in link_checkout_mp(resp, "test")


def test_vip_ainda_valido_respeita_30_dias():
    agora = datetime(2026, 8, 23, 12, 0, 0)
    assert vip_ainda_valido("pago", agora + timedelta(days=30), agora)
    assert not vip_ainda_valido("pago", agora - timedelta(seconds=1), agora)
    assert not vip_ainda_valido("pago", None, agora)
    assert not vip_ainda_valido("expirado", agora + timedelta(days=10), agora)
    assert not vip_ainda_valido("pendente", agora + timedelta(days=10), agora)
