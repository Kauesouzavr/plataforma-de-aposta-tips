import os
import secrets
import random
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, session, jsonify, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import psycopg2.extras
import mercadopago
import requests

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

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
if not RESEND_API_KEY:
    raise RuntimeError("Defina a variável de ambiente RESEND_API_KEY antes de rodar o app.")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Sinal Verde <onboarding@resend.dev>")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Defina a variável de ambiente DATABASE_URL (connection string do Postgres/Supabase).")

VIDEOS_DIR = os.path.join(os.path.dirname(__file__), "protected_media")

# ---------------- BANCO (PostgreSQL / Supabase) ----------------
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

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
    c.close()
    conn.close()


init_db()

# ---------------- AULAS ----------------
# "arquivo" fica em protected_media/, NUNCA em static/ (static é público sem autenticação)
AULAS = [
    {"id": "aula1", "titulo": "Introdução", "arquivo": "aula1.mp4"},
    {"id": "aula2", "titulo": "Estratégia", "arquivo": "aula2.mp4"},
    {"id": "aula3", "titulo": "Método", "arquivo": "aula3.mp4"},
]
AULAS_POR_ID = {a["id"]: a for a in AULAS}


# ---------------- HOME ----------------
@app.route('/')
def home():
    return render_template('index.html')


# ---------------- REGISTRO ----------------
@app.route('/registrar', methods=['POST'])
def registrar():
    email = request.form.get('email', '').strip().lower()
    senha = request.form.get('senha', '')

    if not email or not senha:
        return "Email e senha são obrigatórios", 400
    if len(senha) < 6:
        return "A senha precisa ter pelo menos 6 caracteres", 400

    senha_hash = generate_password_hash(senha)

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO usuarios (email, senha_hash, status) VALUES (%s, %s, 'pendente')",
            (email, senha_hash)
        )
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        # email já cadastrado - segue pro pagamento mesmo assim
        conn.rollback()
    c.close()
    conn.close()

    return redirect(f'/comprar?email={email}')


# ---------------- PAGAMENTO ----------------
@app.route('/comprar')
def comprar():
    email = request.args.get('email', '').strip().lower()
    if not email:
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
    app.logger.error(f"MP preference response: {preference}")

    response = preference.get("response", {})
    link = response.get("init_point") or response.get("sandbox_init_point")

    if not link:
        return (
            "Erro ao gerar o link de pagamento. "
            f"Resposta do Mercado Pago: {response}"
        ), 500

    return redirect(link)


def marcar_pago(email):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE usuarios SET status='pago' WHERE email=%s", (email,))
    conn.commit()
    c.close()
    conn.close()


# ---------------- CONTROLE DE DISPOSITIVOS (limite de 2 por conta) ----------------
MAX_DISPOSITIVOS = 2


def gerar_device_id():
    return secrets.token_hex(24)


def contar_dispositivos_verificados(email):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS total FROM dispositivos WHERE email=%s AND verificado=TRUE", (email,))
    total = c.fetchone()["total"]
    c.close()
    conn.close()
    return total


def dispositivo_verificado(email, device_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT verificado FROM dispositivos WHERE email=%s AND device_id=%s",
        (email, device_id)
    )
    row = c.fetchone()
    c.close()
    conn.close()
    return bool(row and row["verificado"])


def registrar_dispositivo(email, device_id, verificado=False):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO dispositivos (email, device_id, verificado) VALUES (%s, %s, %s) "
        "ON CONFLICT (email, device_id) DO UPDATE SET verificado=%s",
        (email, device_id, verificado, verificado)
    )
    conn.commit()
    c.close()
    conn.close()


def enviar_codigo_verificacao(email, device_id):
    codigo = ''.join(random.choices('0123456789', k=6))

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO codigos_verificacao (email, device_id, codigo) VALUES (%s, %s, %s)",
        (email, device_id, codigo)
    )
    conn.commit()
    c.close()
    conn.close()

    try:
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": EMAIL_FROM,
                "to": [email],
                "subject": "Seu código de acesso — Sinal Verde",
                "html": (
                    f"<p>Detectamos um login em um novo dispositivo.</p>"
                    f"<p>Seu código de verificação é:</p>"
                    f"<h2>{codigo}</h2>"
                    f"<p>Esse código expira em 10 minutos. Se não foi você, ignore este email.</p>"
                )
            },
            timeout=10
        )
    except Exception as e:
        app.logger.error(f"Erro ao enviar email de verificação: {e}")


def validar_codigo_verificacao(email, device_id, codigo):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT id, criado_em FROM codigos_verificacao "
        "WHERE email=%s AND device_id=%s AND codigo=%s "
        "ORDER BY criado_em DESC LIMIT 1",
        (email, device_id, codigo)
    )
    row = c.fetchone()

    valido = False
    if row and (datetime.now() - row["criado_em"]) < timedelta(minutes=10):
        valido = True

    if valido:
        c.execute("DELETE FROM codigos_verificacao WHERE email=%s AND device_id=%s", (email, device_id))
        conn.commit()

    c.close()
    conn.close()
    return valido


def resolver_email_pago(topic, resource_id):
    """
    Busca o pagamento (ou o pedido) na API do Mercado Pago e devolve o email
    a marcar como pago, ou None se não estiver aprovado / não encontrado.
    O Mercado Pago manda notificações em formatos diferentes dependendo do
    evento (payment, merchant_order, etc.), então tratamos os dois.
    """
    try:
        if topic and "merchant_order" in topic:
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
                # O email do pagador (payer) pode ser diferente do email
                # cadastrado - por exemplo, quando a pessoa paga logada com
                # uma conta Mercado Pago diferente do email que ela usou
                # pra se cadastrar no curso.
                return response.get("external_reference") or (response.get("payer") or {}).get("email")
    except Exception as e:
        app.logger.error(f"Erro ao consultar Mercado Pago (topic={topic}, id={resource_id}): {e}")

    return None


# ---------------- WEBHOOK (fonte confiável, roda no servidor a servidor) ----------------
@app.route('/webhook', methods=['POST'])
def webhook():
    body = request.get_json(silent=True) or {}

    # O Mercado Pago manda o "topic"/"type" e o "id" tanto na query string
    # quanto (às vezes) no corpo JSON, dependendo do tipo de notificação.
    topic = request.args.get('type') or request.args.get('topic') or body.get('type') or body.get('topic')
    resource_id = (
        request.args.get('data.id')
        or request.args.get('id')
        or (body.get('data') or {}).get('id')
        or body.get('id')
    )

    app.logger.info(f"Webhook recebido: topic={topic} resource_id={resource_id} body={body}")

    if resource_id:
        email = resolver_email_pago(topic, resource_id)
        if email:
            marcar_pago(email.strip().lower())

    return "ok", 200


# ---------------- SUCESSO (login automático quando o Mercado Pago confirma o pagamento) ----------------
@app.route('/sucesso')
def sucesso():
    payment_id = request.args.get('payment_id') or request.args.get('collection_id')
    status = request.args.get('status') or request.args.get('collection_status')
    external_reference = request.args.get('external_reference')

    email = None

    if payment_id:
        email = resolver_email_pago('payment', payment_id)

    if not email and status == "approved" and external_reference:
        email = external_reference

    if email:
        email = email.strip().lower()
        marcar_pago(email)
        session['user'] = email

        # Primeiro dispositivo é liberado automaticamente (é o momento da compra)
        device_id = request.cookies.get('device_id') or gerar_device_id()
        registrar_dispositivo(email, device_id, verificado=True)

        resp = redirect('/curso')
        resp.set_cookie('device_id', device_id, max_age=60*60*24*365, httponly=True, samesite='Lax')
        return resp

    # Se não deu pra confirmar automaticamente, manda pro login
    return render_template('sucesso.html')


# ---------------- LOGIN ----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT senha_hash FROM usuarios WHERE email=%s", (email,))
        row = c.fetchone()
        c.close()
        conn.close()

        if not row or not check_password_hash(row["senha_hash"], senha):
            return render_template('login.html', erro="Email ou senha inválidos")

        device_id = request.cookies.get('device_id')

        # Dispositivo já reconhecido e verificado -> login direto
        if device_id and dispositivo_verificado(email, device_id):
            session['user'] = email
            resp = redirect('/curso')
            resp.set_cookie('device_id', device_id, max_age=60*60*24*365, httponly=True, samesite='Lax')
            return resp

        # Dispositivo novo -> checa limite antes de pedir verificação
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

        resp = redirect('/verificar-dispositivo')
        resp.set_cookie('device_id', device_id, max_age=60*60*24*365, httponly=True, samesite='Lax')
        return resp

    return render_template('login.html')


# ---------------- VERIFICAÇÃO DE DISPOSITIVO NOVO ----------------
@app.route('/verificar-dispositivo', methods=['GET', 'POST'])
def verificar_dispositivo():
    email = session.get('pendente_email')
    device_id = session.get('pendente_device')

    if not email or not device_id:
        return redirect('/login')

    if request.method == 'POST':
        codigo = request.form.get('codigo', '').strip()

        if validar_codigo_verificacao(email, device_id, codigo):
            registrar_dispositivo(email, device_id, verificado=True)
            session.pop('pendente_email', None)
            session.pop('pendente_device', None)
            session['user'] = email
            return redirect('/curso')

        return render_template('verificar_dispositivo.html', erro="Código inválido ou expirado.", email=email)

    return render_template('verificar_dispositivo.html', email=email)


@app.route('/verificar-dispositivo/reenviar', methods=['POST'])
def reenviar_codigo():
    email = session.get('pendente_email')
    device_id = session.get('pendente_device')

    if email and device_id:
        enviar_codigo_verificacao(email, device_id)

    return redirect('/verificar-dispositivo')


# ---------------- CURSO ----------------
@app.route('/curso')
def curso():
    if 'user' not in session:
        return redirect('/login')

    email = session['user']

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT status FROM usuarios WHERE email=%s", (email,))
    row = c.fetchone()

    if not row or row["status"] != "pago":
        c.close()
        conn.close()
        return render_template('login.html', erro="Não encontramos um pagamento aprovado para essa conta.")

    c.execute("SELECT aula FROM progresso WHERE email=%s", (email,))
    vistas = [r["aula"] for r in c.fetchall()]

    c.execute("SELECT * FROM palpites ORDER BY criado_em DESC LIMIT 20")
    palpites = c.fetchall()

    c.close()
    conn.close()

    return render_template('curso.html', aulas=AULAS, vistas=vistas, palpites=palpites, email=email)


# ---------------- VÍDEO (única forma de acessar o arquivo .mp4, sempre autenticada) ----------------
@app.route('/video/<aula_id>')
def video(aula_id):
    if 'user' not in session:
        abort(403)

    email = session['user']

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT status FROM usuarios WHERE email=%s", (email,))
    row = c.fetchone()
    c.close()
    conn.close()

    if not row or row["status"] != "pago":
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
    aula = (request.json or {}).get('aula')

    if aula not in AULAS_POR_ID:
        return jsonify({"ok": False}), 400

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO progresso (email, aula) VALUES (%s, %s) ON CONFLICT (email, aula) DO NOTHING",
        (email, aula)
    )
    conn.commit()
    c.close()
    conn.close()

    return jsonify({"ok": True})


# ---------------- PAINEL DE PALPITES (senha própria, separada do /admin) ----------------
@app.route('/painel/login', methods=['GET', 'POST'])
def painel_login():
    if request.method == 'POST':
        senha = request.form.get('senha', '')
        if senha == PALPITES_PASSWORD:
            session['operador'] = True
            return redirect('/painel')
        return render_template('painel_login.html', erro="Senha incorreta")

    return render_template('painel_login.html')


@app.route('/painel', methods=['GET', 'POST'])
def painel():
    if not session.get('operador'):
        return redirect('/painel/login')

    if request.method == 'POST':
        competicao = request.form.get('competicao', '').strip()
        confronto = request.form.get('confronto', '').strip()
        mercado = request.form.get('mercado', '').strip()
        odd = request.form.get('odd', '').strip()
        link = request.form.get('link', '').strip()
        hot = request.form.get('hot') == 'on'

        if competicao and confronto and mercado and odd and link:
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "INSERT INTO palpites (competicao, confronto, mercado, odd, link, hot) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (competicao, confronto, mercado, odd, link, hot)
            )
            conn.commit()
            c.close()
            conn.close()

        return redirect('/painel')

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM palpites ORDER BY criado_em DESC LIMIT 50")
    palpites = c.fetchall()
    c.close()
    conn.close()

    return render_template('painel.html', palpites=palpites)


@app.route('/painel/excluir/<int:palpite_id>', methods=['POST'])
def painel_excluir(palpite_id):
    if not session.get('operador'):
        return redirect('/painel/login')

    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM palpites WHERE id=%s", (palpite_id,))
    conn.commit()
    c.close()
    conn.close()

    return redirect('/painel')


@app.route('/painel/logout')
def painel_logout():
    session.pop('operador', None)
    return redirect('/')


# ---------------- ADMIN (agora exige senha) ----------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        senha = request.form.get('senha', '')
        if senha == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect('/admin')
        return render_template('admin_login.html', erro="Senha incorreta")

    return render_template('admin_login.html')


@app.route('/admin')
def admin():
    if not session.get('admin'):
        return redirect('/admin/login')

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT email, status FROM usuarios")
    usuarios_rows = c.fetchall()

    c.execute("SELECT email, COUNT(*) AS total FROM dispositivos WHERE verificado=TRUE GROUP BY email")
    dispositivos_por_email = {r["email"]: r["total"] for r in c.fetchall()}

    usuarios = [(r["email"], r["status"], dispositivos_por_email.get(r["email"], 0)) for r in usuarios_rows]

    c.execute("SELECT email, COUNT(aula) FROM progresso GROUP BY email")
    progresso_rows = c.fetchall()
    progresso = [(r["email"], r["count"]) for r in progresso_rows]

    c.close()
    conn.close()

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
    if email:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM dispositivos WHERE email=%s", (email,))
        conn.commit()
        c.close()
        conn.close()

    return redirect('/admin')


# ---------------- LOGOUT ----------------
@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/')


if __name__ == '__main__':
    app.run(debug=False)