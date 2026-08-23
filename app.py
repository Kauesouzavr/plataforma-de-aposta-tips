import os
import re
import hmac
import time
import secrets
import requests
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, render_template, request, redirect, session, jsonify, send_from_directory, abort
from flask_wtf.csrf import CSRFProtect
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import psycopg2.extras
import mercadopago

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# ---------------- CONFIG (tudo via variável de ambiente) ----------------
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("Defina a variável de ambiente SECRET_KEY antes de rodar o app.")

MP_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
if not MP_TOKEN:
    raise RuntimeError("Defina a variável de ambiente MP_ACCESS_TOKEN antes de rodar o app.")
sdk = mercadopago.SDK(MP_TOKEN)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError("Defina a variável de ambiente ADMIN_PASSWORD antes de rodar o app.")

PALPITES_PASSWORD = os.environ.get("PALPITES_PASSWORD")
if not PALPITES_PASSWORD:
    raise RuntimeError("Defina a variável de ambiente PALPITES_PASSWORD antes de rodar o app.")

BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
if not BREVO_API_KEY:
    raise RuntimeError("Defina a variável de ambiente BREVO_API_KEY antes de rodar o app.")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL")
if not BREVO_SENDER_EMAIL:
    raise RuntimeError("Defina a variável de ambiente BREVO_SENDER_EMAIL antes de rodar o app.")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
IS_HTTPS = BASE_URL.startswith("https://")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Defina a variável de ambiente DATABASE_URL (connection string do Postgres/Supabase).")

VIDEOS_DIR = os.path.join(os.path.dirname(__file__), "protected_media")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_DISPOSITIVOS = 2
MAX_TENTATIVAS_CODIGO = 5

app.config.update(
    WTF_CSRF_TIME_LIMIT=None,
    WTF_CSRF_SSL_STRICT=IS_HTTPS,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_HTTPS,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)

csrf = CSRFProtect(app)

# ---------------- RATE LIMIT (por processo / worker) ----------------
_rate_hits = defaultdict(list)


def rate_limit(max_hits, seconds):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.method not in ("POST", "PUT", "PATCH"):
                return view(*args, **kwargs)
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
            key = f"{view.__name__}:{ip}"
            agora = time.time()
            recentes = [t for t in _rate_hits[key] if agora - t < seconds]
            if len(recentes) >= max_hits:
                return "Muitas tentativas. Espere um pouco e tente de novo.", 429
            recentes.append(agora)
            _rate_hits[key] = recentes
            return view(*args, **kwargs)
        return wrapped
    return decorator


def senha_confere(informada, esperada):
    if not informada or not esperada:
        return False
    return hmac.compare_digest(informada, esperada)


def email_valido(email):
    return bool(email) and len(email) <= 150 and EMAIL_RE.match(email)


def link_https_seguro(url):
    """Só aceita https://host/... — bloqueia javascript:, data: e http."""
    if not url or len(url) > 500:
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return None
    return url.strip()


def cookie_device(resp, device_id):
    resp.set_cookie(
        "device_id",
        device_id,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="Lax",
        secure=IS_HTTPS,
    )
    return resp


# ---------------- BANCO (PostgreSQL / Supabase) ----------------
@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute('''
                CREATE TABLE IF NOT EXISTS usuarios (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    senha_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pendente'
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS progresso (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    aula TEXT NOT NULL,
                    UNIQUE(email, aula)
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS palpites (
                    id SERIAL PRIMARY KEY,
                    competicao TEXT NOT NULL,
                    confronto TEXT NOT NULL,
                    mercado TEXT NOT NULL,
                    odd TEXT NOT NULL,
                    link TEXT NOT NULL,
                    hot BOOLEAN NOT NULL DEFAULT FALSE,
                    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS dispositivos (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    verificado BOOLEAN NOT NULL DEFAULT FALSE,
                    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
                    UNIQUE(email, device_id)
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS codigos_verificacao (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    codigo TEXT NOT NULL,
                    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
                )
            ''')
        conn.commit()


if os.environ.get("SKIP_INIT_DB") != "1":
    init_db()

# ---------------- AULAS ----------------
# "arquivo" fica em protected_media/, NUNCA em static/ (static é público sem autenticação)
AULAS = [
    {"id": "aula1", "titulo": "Introdução", "arquivo": "aula1.mp4"},
    {"id": "aula2", "titulo": "Estratégia", "arquivo": "aula2.mp4"},
    {"id": "aula3", "titulo": "Método", "arquivo": "aula3.mp4"},
]
AULAS_POR_ID = {a["id"]: a for a in AULAS}


def usuario_pago(email):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT status FROM usuarios WHERE email=%s", (email,))
            row = c.fetchone()
    return bool(row and row["status"] == "pago")


# ---------------- HOME ----------------
@app.context_processor
def injetar_globais():
    return {"BASE_URL": BASE_URL}


@app.route('/')
def home():
    return render_template('index.html')


# ---------------- SEO / ARQUIVOS TECNICOS ----------------
@app.route('/robots.txt')
def robots_txt():
    conteudo = (
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /painel\n"
        "Disallow: /curso\n"
        "Disallow: /verificar-dispositivo\n"
        "Allow: /\n"
        f"Sitemap: {BASE_URL}/sitemap.xml\n"
    )
    return conteudo, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route('/sitemap.xml')
def sitemap_xml():
    paginas = ["/", "/login", "/politica-de-privacidade", "/contato"]
    urls = "".join(f"<url><loc>{BASE_URL}{p}</loc></url>" for p in paginas)
    conteudo = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return conteudo, 200, {"Content-Type": "application/xml; charset=utf-8"}


# ---------------- PAGINAS INSTITUCIONAIS ----------------
@app.route('/politica-de-privacidade')
def politica_privacidade():
    return render_template('politica_privacidade.html')


@app.route('/contato', methods=['GET', 'POST'])
@rate_limit(5, 60)
def contato():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()[:100]
        email_contato = request.form.get('email', '').strip()[:150]
        mensagem = request.form.get('mensagem', '').strip()[:1000]

        if nome and email_valido(email_contato) and mensagem:
            try:
                requests.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={
                        "api-key": BREVO_API_KEY,
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    },
                    json={
                        "sender": {"email": BREVO_SENDER_EMAIL, "name": "JV Tips - Contato"},
                        "to": [{"email": BREVO_SENDER_EMAIL}],
                        "replyTo": {"email": email_contato, "name": nome},
                        "subject": f"Nova mensagem de contato: {escape(nome)}",
                        "htmlContent": (
                            f"<p><strong>Nome:</strong> {escape(nome)}</p>"
                            f"<p><strong>Email:</strong> {escape(email_contato)}</p>"
                            f"<p><strong>Mensagem:</strong></p>"
                            f"<p>{escape(mensagem)}</p>"
                        )
                    },
                    timeout=10
                )
            except Exception as e:
                app.logger.error("Erro ao enviar mensagem de contato: %s", e)

            return redirect('/contato/obrigado')

    return render_template('contato.html')


@app.route('/contato/obrigado')
def contato_obrigado():
    return render_template('contato_obrigado.html')


# ---------------- REGISTRO ----------------
@app.route('/registrar', methods=['POST'])
@rate_limit(5, 60)
def registrar():
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '')

    if not email_valido(email) or not senha:
        return "Email e senha são obrigatórios", 400
    if len(senha) < 6:
        return "A senha precisa ter pelo menos 6 caracteres", 400

    senha_hash = generate_password_hash(senha)

    with get_db() as conn:
        with conn.cursor() as c:
            try:
                c.execute(
                    "INSERT INTO usuarios (email, senha_hash, status) VALUES (%s, %s, 'pendente')",
                    (email, senha_hash)
                )
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()

    return redirect(f'/comprar?email={email}')


# ---------------- PAGAMENTO ----------------
@app.route('/comprar')
def comprar():
    email = request.args.get('email', '').strip().lower()
    if not email_valido(email):
        return redirect('/')

    preference_data = {
        "items": [{
            "title": "Mini Curso",
            "quantity": 1,
            "currency_id": "BRL",
            "unit_price": 29.90
        }],
        "payer": {"email": email},
        "external_reference": email,
        "back_urls": {
            "success": f"{BASE_URL}/sucesso",
            "failure": f"{BASE_URL}/",
            "pending": f"{BASE_URL}/",
        },
        "auto_return": "approved",
        "notification_url": f"{BASE_URL}/webhook"
    }

    preference = sdk.preference().create(preference_data)
    status_pref = preference.get("status")
    response = preference.get("response", {}) or {}
    link = response.get("init_point") or response.get("sandbox_init_point")

    if not link:
        app.logger.error(
            "Erro ao gerar preferência Mercado Pago (http_status=%s, message=%s)",
            status_pref,
            response.get("message") or response.get("error"),
        )
        return "Erro ao gerar o link de pagamento. Tente novamente em instantes.", 500

    return redirect(link)


def marcar_pago(email):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE usuarios SET status='pago' WHERE email=%s", (email,))
        conn.commit()


def gerar_device_id():
    return secrets.token_hex(24)


def contar_dispositivos_verificados(email):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS total FROM dispositivos WHERE email=%s AND verificado=TRUE",
                (email,),
            )
            total = c.fetchone()["total"]
    return total


def dispositivo_verificado(email, device_id):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT verificado FROM dispositivos WHERE email=%s AND device_id=%s",
                (email, device_id)
            )
            row = c.fetchone()
    return bool(row and row["verificado"])


def registrar_dispositivo(email, device_id, verificado=False):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO dispositivos (email, device_id, verificado) VALUES (%s, %s, %s) "
                "ON CONFLICT (email, device_id) DO UPDATE SET verificado=%s",
                (email, device_id, verificado, verificado)
            )
        conn.commit()


def enviar_codigo_verificacao(email, device_id):
    codigo = f"{secrets.randbelow(1_000_000):06d}"

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute(
                "DELETE FROM codigos_verificacao WHERE email=%s AND device_id=%s",
                (email, device_id),
            )
            c.execute(
                "INSERT INTO codigos_verificacao (email, device_id, codigo) VALUES (%s, %s, %s)",
                (email, device_id, codigo)
            )
        conn.commit()

    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            json={
                "sender": {"email": BREVO_SENDER_EMAIL, "name": "JV Tips"},
                "to": [{"email": email}],
                "subject": "Seu código de acesso JV Tips",
                "htmlContent": (
                    "<p>Detectamos um login em um novo dispositivo.</p>"
                    "<p>Seu código de verificação é:</p>"
                    f"<h2>{escape(codigo)}</h2>"
                    "<p>Esse código expira em 10 minutos. Se não foi você, ignore este email.</p>"
                )
            },
            timeout=10
        )
        if resp.status_code >= 400:
            app.logger.error("Brevo recusou email de verificação (status=%s)", resp.status_code)
    except Exception as e:
        app.logger.error("Erro ao enviar email de verificação: %s", e)


def validar_codigo_verificacao(email, device_id, codigo):
    if not codigo or not codigo.isdigit() or len(codigo) != 6:
        return False

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT id FROM codigos_verificacao "
                "WHERE email=%s AND device_id=%s AND codigo=%s "
                "AND criado_em > (NOW() - INTERVAL '10 minutes') "
                "ORDER BY criado_em DESC LIMIT 1",
                (email, device_id, codigo)
            )
            row = c.fetchone()
            if not row:
                return False
            c.execute(
                "DELETE FROM codigos_verificacao WHERE email=%s AND device_id=%s",
                (email, device_id),
            )
        conn.commit()
    return True


def resolver_email_pago(topic, resource_id):
    """
    Busca o pagamento (ou o pedido) na API do Mercado Pago e devolve o email
    a marcar como pago, ou None se não estiver aprovado / não encontrado.
    O Mercado Pago manda notificações em formatos diferentes dependendo do
    evento (payment, merchant_order, etc.), então tratamos os dois.
    """
    try:
        if topic and "merchant_order" in str(topic):
            order = sdk.merchant_order().get(resource_id)
            response = order["response"]
            pagamentos = response.get("payments", [])
            aprovado = any(p.get("status") == "approved" for p in pagamentos)
            if aprovado:
                return response.get("external_reference")
        else:
            payment = sdk.payment().get(resource_id)
            response = payment["response"]
            if response.get("status") == "approved":
                # Prioriza o email cadastrado no SITE (external_reference).
                return response.get("external_reference") or (response.get("payer") or {}).get("email")
    except Exception as e:
        app.logger.error("Erro ao consultar Mercado Pago (topic=%s): %s", topic, e)

    return None


# ---------------- WEBHOOK (fonte confiável, roda no servidor a servidor) ----------------
@app.route('/webhook', methods=['POST'])
@csrf.exempt
def webhook():
    body = request.get_json(silent=True) or {}

    topic = request.args.get('type') or request.args.get('topic') or body.get('type') or body.get('topic')
    resource_id = (
        request.args.get('data.id')
        or request.args.get('id')
        or (body.get('data') or {}).get('id')
        or body.get('id')
    )

    app.logger.info("Webhook recebido: topic=%s resource_id=%s", topic, resource_id)

    if resource_id:
        email = resolver_email_pago(topic, resource_id)
        if email and email_valido(email.strip().lower()):
            marcar_pago(email.strip().lower())

    return "ok", 200


# ---------------- SUCESSO (só libera se a API do Mercado Pago confirmar) ----------------
@app.route('/sucesso')
def sucesso():
    payment_id = request.args.get('payment_id') or request.args.get('collection_id')

    # Nunca confiar em status/external_reference da URL — qualquer um pode forjar.
    if not payment_id:
        return render_template('sucesso.html')

    email = resolver_email_pago('payment', payment_id)
    if not email or not email_valido(email.strip().lower()):
        return render_template('sucesso.html')

    email = email.strip().lower()
    marcar_pago(email)
    session.permanent = True
    session['user'] = email

    device_id = request.cookies.get('device_id') or gerar_device_id()
    registrar_dispositivo(email, device_id, verificado=True)

    resp = redirect('/curso')
    return cookie_device(resp, device_id)


# ---------------- LOGIN ----------------
@app.route('/login', methods=['GET', 'POST'])
@rate_limit(10, 60)
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT senha_hash, status FROM usuarios WHERE email=%s", (email,))
                row = c.fetchone()

        if not row or not check_password_hash(row["senha_hash"], senha):
            return render_template('login.html', erro="Email ou senha inválidos")

        if row["status"] != "pago":
            return render_template(
                'login.html',
                erro="Não encontramos um pagamento aprovado para essa conta. "
                     "Se você acabou de pagar, aguarde alguns segundos e tente de novo.",
            )

        device_id = request.cookies.get('device_id')

        if device_id and dispositivo_verificado(email, device_id):
            session.permanent = True
            session['user'] = email
            resp = redirect('/curso')
            return cookie_device(resp, device_id)

        if not device_id:
            device_id = gerar_device_id()

        if contar_dispositivos_verificados(email) >= MAX_DISPOSITIVOS:
            return render_template(
                'login.html',
                erro=f"Limite de {MAX_DISPOSITIVOS} dispositivos atingido nessa conta. "
                     f"Fale com o suporte pra liberar um novo aparelho."
            )

        registrar_dispositivo(email, device_id, verificado=False)
        enviar_codigo_verificacao(email, device_id)

        session['pendente_email'] = email
        session['pendente_device'] = device_id
        session['codigo_tentativas'] = 0

        resp = redirect('/verificar-dispositivo')
        return cookie_device(resp, device_id)

    return render_template('login.html')


# ---------------- VERIFICAÇÃO DE DISPOSITIVO NOVO ----------------
@app.route('/verificar-dispositivo', methods=['GET', 'POST'])
@rate_limit(10, 60)
def verificar_dispositivo():
    email = session.get('pendente_email')
    device_id = session.get('pendente_device')

    if not email or not device_id:
        return redirect('/login')

    if request.method == 'POST':
        tentativas = session.get('codigo_tentativas', 0) + 1
        session['codigo_tentativas'] = tentativas
        if tentativas > MAX_TENTATIVAS_CODIGO:
            session.pop('pendente_email', None)
            session.pop('pendente_device', None)
            session.pop('codigo_tentativas', None)
            return render_template(
                'login.html',
                erro="Muitas tentativas de código. Faça login de novo para receber outro.",
            )

        codigo = request.form.get('codigo', '').strip()

        if validar_codigo_verificacao(email, device_id, codigo):
            registrar_dispositivo(email, device_id, verificado=True)
            session.pop('pendente_email', None)
            session.pop('pendente_device', None)
            session.pop('codigo_tentativas', None)
            session.permanent = True
            session['user'] = email
            return redirect('/curso')

        return render_template('verificar_dispositivo.html', erro="Código inválido ou expirado.", email=email)

    return render_template('verificar_dispositivo.html', email=email)


@app.route('/verificar-dispositivo/reenviar', methods=['POST'])
@rate_limit(3, 60)
def reenviar_codigo():
    email = session.get('pendente_email')
    device_id = session.get('pendente_device')

    if email and device_id:
        enviar_codigo_verificacao(email, device_id)
        session['codigo_tentativas'] = 0

    return redirect('/verificar-dispositivo')


# ---------------- CURSO ----------------
@app.route('/curso')
def curso():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']

    if not usuario_pago(email):
        session.pop('user', None)
        return render_template('login.html', erro="Não encontramos um pagamento aprovado para essa conta.")

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT aula FROM progresso WHERE email=%s", (email,))
            vistas = [r["aula"] for r in c.fetchall()]
            c.execute("SELECT * FROM palpites ORDER BY criado_em DESC LIMIT 20")
            palpites = c.fetchall()

    return render_template('curso.html', aulas=AULAS, vistas=vistas, palpites=palpites, email=email)


# ---------------- VÍDEO (única forma de acessar o arquivo .mp4, sempre autenticada) ----------------
@app.route('/video/<aula_id>')
def video(aula_id):
    if 'user' not in session:
        abort(403)

    email = session['user']
    if not usuario_pago(email):
        abort(403)

    aula = AULAS_POR_ID.get(aula_id)
    if not aula:
        abort(404)

    return send_from_directory(VIDEOS_DIR, aula["arquivo"])


# ---------------- MARCAR AULA ----------------
@app.route('/marcar', methods=['POST'])
def marcar():
    if 'user' not in session:
        return jsonify({"ok": False}), 403

    email = session['user']
    if not usuario_pago(email):
        return jsonify({"ok": False}), 403

    aula = (request.json or {}).get('aula')

    if aula not in AULAS_POR_ID:
        return jsonify({"ok": False}), 400

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO progresso (email, aula) VALUES (%s, %s) ON CONFLICT (email, aula) DO NOTHING",
                (email, aula)
            )
        conn.commit()

    return jsonify({"ok": True})


# ---------------- PAINEL DE PALPITES (senha própria, separada do /admin) ----------------
@app.route('/painel/login', methods=['GET', 'POST'])
@rate_limit(10, 60)
def painel_login():
    if request.method == 'POST':
        senha = request.form.get('senha', '')
        if senha_confere(senha, PALPITES_PASSWORD):
            session['operador'] = True
            return redirect('/painel')
        return render_template('painel_login.html', erro="Senha incorreta")

    return render_template('painel_login.html')


@app.route('/painel', methods=['GET', 'POST'])
def painel():
    if not session.get('operador'):
        return redirect('/painel/login')

    if request.method == 'POST':
        competicao = request.form.get('competicao', '').strip()[:100]
        confronto = request.form.get('confronto', '').strip()[:100]
        mercado = request.form.get('mercado', '').strip()[:100]
        odd = request.form.get('odd', '').strip()[:10]
        link = link_https_seguro(request.form.get('link', ''))
        hot = request.form.get('hot') == 'on'

        if competicao and confronto and mercado and odd and link:
            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute(
                        "INSERT INTO palpites (competicao, confronto, mercado, odd, link, hot) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (competicao, confronto, mercado, odd, link, hot)
                    )
                conn.commit()

        return redirect('/painel')

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM palpites ORDER BY criado_em DESC LIMIT 50")
            palpites = c.fetchall()

    return render_template('painel.html', palpites=palpites)


@app.route('/painel/excluir/<int:palpite_id>', methods=['POST'])
def painel_excluir(palpite_id):
    if not session.get('operador'):
        return redirect('/painel/login')

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM palpites WHERE id=%s", (palpite_id,))
        conn.commit()

    return redirect('/painel')


@app.route('/painel/logout')
def painel_logout():
    session.pop('operador', None)
    return redirect('/')


# ---------------- ADMIN (agora exige senha) ----------------
@app.route('/admin/login', methods=['GET', 'POST'])
@rate_limit(10, 60)
def admin_login():
    if request.method == 'POST':
        senha = request.form.get('senha', '')
        if senha_confere(senha, ADMIN_PASSWORD):
            session['admin'] = True
            return redirect('/admin')
        return render_template('admin_login.html', erro="Senha incorreta")

    return render_template('admin_login.html')


@app.route('/admin')
def admin():
    if not session.get('admin'):
        return redirect('/admin/login')

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT email, status FROM usuarios")
            usuarios_rows = c.fetchall()

            c.execute("SELECT email, COUNT(*) AS total FROM dispositivos WHERE verificado=TRUE GROUP BY email")
            dispositivos_por_email = {r["email"]: r["total"] for r in c.fetchall()}

            usuarios = [(r["email"], r["status"], dispositivos_por_email.get(r["email"], 0)) for r in usuarios_rows]

            c.execute("SELECT email, COUNT(aula) FROM progresso GROUP BY email")
            progresso_rows = c.fetchall()
            progresso = [(r["email"], r["count"]) for r in progresso_rows]

    return render_template('admin.html', usuarios=usuarios, progresso=progresso)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect('/')


@app.route('/admin/reset-dispositivos', methods=['POST'])
def admin_reset_dispositivos():
    if not session.get('admin'):
        return redirect('/admin/login')

    email = request.form.get('email', '').strip().lower()
    if email_valido(email):
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("DELETE FROM dispositivos WHERE email=%s", (email,))
            conn.commit()

    return redirect('/admin')


# ---------------- LOGOUT ----------------
@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/')


# ---------------- ERROS ----------------
@app.errorhandler(404)
def erro_404(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def erro_500(e):
    return render_template('500.html'), 500


if __name__ == '__main__':
    app.run(debug=False)
