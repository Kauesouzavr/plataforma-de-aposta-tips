import os
from flask import Flask, render_template, request, redirect, session, jsonify, send_from_directory, abort
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
                return (response.get("payer") or {}).get("email") or response.get("external_reference")
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
        return redirect('/curso')
 
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
 
        if row and check_password_hash(row["senha_hash"], senha):
            session['user'] = email
            return redirect('/curso')
 
        return render_template('login.html', erro="Email ou senha inválidos")
 
    return render_template('login.html')
 
 
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
    c.close()
    conn.close()
 
    return render_template('curso.html', aulas=AULAS, vistas=vistas)
 
 
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
    usuarios = [(r["email"], r["status"]) for r in usuarios_rows]
 
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
 
 
# ---------------- LOGOUT ----------------
@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/')
 
 
if __name__ == '__main__':
    app.run(debug=False)
 