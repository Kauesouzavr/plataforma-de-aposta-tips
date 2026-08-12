# Mini Curso — versão corrigida

## O que mudou

1. **Senhas com hash** (`werkzeug.security`) — antes ficavam em texto puro no banco.
2. **`/admin` protegido** por senha (`/admin/login`) — antes era público pra qualquer um.
3. **Vídeos fora do `/static`** — agora ficam em `protected_media/` e só são entregues pela rota `/video/<aula_id>`, que confere login + pagamento antes de liberar o arquivo. Antes, `/static/aula1.mp4` etc. estavam acessíveis sem login.
4. **Login automático após pagamento aprovado** — a página `/sucesso` agora confirma o pagamento direto com o Mercado Pago e já loga o usuário, em vez de deixar ele "solto".
5. **`/marcar` não duplica mais** — a tabela `progresso` agora tem `UNIQUE(email, aula)` e o insert usa `ON CONFLICT DO NOTHING`.
6. **Nenhuma credencial no código** — `SECRET_KEY`, `MP_ACCESS_TOKEN`, `ADMIN_PASSWORD`, `BASE_URL` e `DATABASE_URL` agora vêm de variáveis de ambiente. O app nem sobe se elas não estiverem definidas (isso é proposital, pra você não esquecer de configurar).
7. **Banco migrado de SQLite para PostgreSQL** (Supabase) — o Render (no plano free) não tem disco persistente, então um `database.db` local se perdia a cada deploy. Agora o banco vive fora do serviço web, em um Postgres gerenciado, e sobrevive a qualquer deploy/restart.

## Passo a passo para colocar no ar

### 1. Gere um novo token do Mercado Pago
O token antigo (`APP_USR-8625223623593145-...`) apareceu em texto puro nesta conversa, então deve ser tratado como comprometido. Vá no painel do Mercado Pago (Suas integrações > Credenciais de produção) e gere um novo. Revogue o antigo.

### 2. Mova os vídeos
No projeto antigo, `aula1.mp4`, `aula2.mp4`, `aula3.mp4` estavam em `static/`. Nesta versão eles precisam ir para a pasta `protected_media/` (na raiz do projeto, do lado de `app.py`), **não** dentro de `static/`. Copie os 3 arquivos .mp4 pra essa pasta.

### 3. Configure as variáveis de ambiente
Copie `.env.example` para `.env` (uso local) e preencha:
- `SECRET_KEY`: qualquer string longa e aleatória (ex: gere com `python -c "import secrets; print(secrets.token_hex(32))"`)
- `MP_ACCESS_TOKEN`: o novo token do passo 1
- `ADMIN_PASSWORD`: senha forte pra você acessar `/admin`
- `BASE_URL`: `https://curso-yvqf.onrender.com` (sem barra no final)
- `DATABASE_URL`: a connection string do seu banco Postgres (Supabase > Connect > Session pooler > Type: URI)

No Render, configure essas mesmas variáveis em **Settings > Environment** do serviço — não suba o arquivo `.env` pro Git.

### 4. Banco de dados
O app agora usa **PostgreSQL** (ex: Supabase) em vez de SQLite, porque o Render no plano free não tem disco persistente — o `database.db` local se perdia a cada deploy. A tabela é criada automaticamente na primeira vez que o app sobe (`init_db()`), então não precisa rodar nenhum script manual: basta a `DATABASE_URL` estar configurada corretamente.

Como o schema mudou (senha agora é `senha_hash`, e a tabela `progresso` tem `UNIQUE(email, aula)`), e o banco novo no Supabase começa vazio, os usuários que porventura já tinham se cadastrado no SQLite antigo vão precisar se cadastrar de novo.

### 5. Suba o código
Substitua o `app.py`, a pasta `templates/`, `static/style.css` e `requirements.txt` do seu repositório pelos desses arquivos. Confirme que `protected_media/` está no `.gitignore` se você não quiser os vídeos versionados no Git (arquivos de vídeo grandes não devem ir pro Git de qualquer forma — considere um serviço de storage como S3/Cloudflare R2 se os vídeos crescerem).

### 6. Teste local
```
pip install -r requirements.txt
export SECRET_KEY=teste
export MP_ACCESS_TOKEN=seu-token-de-teste
export ADMIN_PASSWORD=admin123
export BASE_URL=http://localhost:5000
python app.py
```

## Observação sobre segurança dos vídeos
Mesmo protegido por login, um usuário pago ainda pode baixar o vídeo manualmente (isso é inerente a qualquer vídeo `<video>` no navegador — não existe proteção 100% client-side). O que a correção resolve é impedir que **pessoas que não pagaram** acessem o arquivo. Se quiser dificultar ainda mais o compartilhamento do link entre pessoas pagantes, dá pra evoluir depois para streaming com tokens de curta duração ou um provedor de vídeo com DRM (Vimeo, Bunny Stream, etc.) — mas isso já é um upgrade de infraestrutura, não uma correção de bug.
