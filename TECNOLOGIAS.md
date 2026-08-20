# JV Tips — Descrição completa do projeto

Documento com tudo que foi usado para construir, proteger e hospedar o site, do início ao fim.

## O que o site faz

Plataforma de assinatura (VIP) para análises e palpites esportivos. A pessoa se cadastra, paga uma mensalidade via Mercado Pago, e ganha acesso a uma área exclusiva com palpites publicados diariamente por um operador autorizado, além de um vídeo de boas-vindas explicando o funcionamento.

---

## Backend

| Item | Uso |
|---|---|
| **Python 3.11** | Linguagem principal do servidor |
| **Flask 3.0.3** | Framework web que roda todas as rotas (`/`, `/login`, `/curso`, `/admin`, `/painel`, etc.) |
| **Gunicorn** | Servidor de produção que executa o Flask no Render (definido no `Procfile`) |
| **psycopg2-binary** | Driver de conexão com o banco PostgreSQL |
| **python-dotenv** | Carrega variáveis de ambiente de um arquivo `.env` quando rodado localmente |
| **requests** | Usado para chamar a API do Brevo (envio de email) |
| **werkzeug.security** | Faz o hash das senhas dos usuários (nunca fica texto puro no banco) |
| **mercadopago (SDK oficial)** | Cria as preferências de pagamento e consulta status de pagamentos/pedidos |

## Banco de dados

- **PostgreSQL**, hospedado gratuitamente no **Supabase**
- Conectado via *Session Pooler* (compatível com redes IPv4, que é o caso do Render)
- Tabelas:
  - `usuarios` — email, senha (hash), status (pendente/pago)
  - `progresso` — controle de quais vídeos cada usuário já assistiu
  - `palpites` — os palpites publicados (competição, confronto, mercado, odd, link, destaque)
  - `dispositivos` — controla quais aparelhos cada conta já usou para logar (limite de 2)
  - `codigos_verificacao` — códigos temporários de 6 dígitos para liberar dispositivo novo

## Pagamentos

- **Mercado Pago — Checkout Pro**
  - Preferência de pagamento criada dinamicamente por usuário
  - Webhook (`/webhook`) recebe a confirmação em tempo real, tratando os dois formatos de notificação que o Mercado Pago envia (`payment` e `merchant_order`)
  - Login automático da pessoa assim que o pagamento é aprovado (via `/sucesso`)
  - Ambiente de teste (sandbox) usado para validar o fluxo com contas e cartões de teste antes de liberar em produção

## Envio de email

- **Brevo** (antigo Sendinblue) — API de email transacional
  - Usado para mandar o código de verificação de dispositivo novo
  - Escolhido depois de testar **Resend** (só permitia mandar para o próprio email sem domínio verificado) e **Gmail via SMTP** (bloqueado pelo Render no plano gratuito)
  - Funciona via HTTPS, então não é afetado pelo bloqueio de portas SMTP do Render

## Hospedagem

- **Render** — hospeda o site (web service)
  - Deploy automático a cada `git push` na branch `main`
  - Variáveis de ambiente configuradas lá (nenhuma credencial fica no código)
  - Limitações do plano gratuito identificadas: hibernação por inatividade e bloqueio de portas SMTP

## Frontend

- **HTML + CSS + JavaScript puro** (sem framework de frontend nem processo de build)
- **Jinja2** — motor de templates do Flask, usado para gerar o HTML dinâmico
- **Google Fonts** — Archivo Black, Inter, JetBrains Mono
- Efeitos visuais construídos do zero, sem bibliotecas externas de animação/3D:
  - Objetos girando em 3D (bola de basquete, bola de futebol, ficha) feitos só com CSS (gradientes, sombras internas, `transform-style: preserve-3d`)
  - Cards com inclinação 3D que segue o mouse (`perspective` + `rotateX/rotateY` via JavaScript)
  - Tela de introdução com contador e efeito de "cortina" abrindo
  - Animações de entrada ao rolar a página (`IntersectionObserver`)
  - Cursor customizado (desativado automaticamente em celulares)
  - Marca d'água dinâmica com o email do usuário logado (proteção contra print/compartilhamento)

## Segurança implementada

- Senhas com hash (`werkzeug.security`, algoritmo scrypt)
- Sessões assinadas com `SECRET_KEY` própria
- Login de admin (`/admin`) e login do painel de palpites (`/painel`) com senhas **separadas**, para limitar o que cada pessoa consegue ver
- Limite de **2 dispositivos por conta**, com bloqueio de um terceiro dispositivo até liberação manual ou expiração
- Verificação por código de 6 dígitos enviado por email ao logar de um dispositivo novo
- Vídeos protegidos: servidos por uma rota autenticada (`/video/<id>`), não por arquivo estático público
- Marca d'água identificando o usuário, para rastrear vazamento de conteúdo
- Nenhuma credencial no código-fonte — tudo em variáveis de ambiente

## Controle de versão e deploy

- **Git** + **GitHub** (repositório `Kauesouzavr/curso`)
- **VS Code** como editor
- Fluxo de trabalho: editar local → `git add` → `git commit` → `git push` → Render faz o deploy automático

## Contas e serviços externos criados durante o projeto

- Mercado Pago (produção + credenciais de teste)
- Supabase (banco de dados)
- Render (hospedagem)
- Brevo (envio de email)
- GitHub (código-fonte)

## Variáveis de ambiente usadas (nomes, sem valores)

```
SECRET_KEY
MP_ACCESS_TOKEN
ADMIN_PASSWORD
PALPITES_PASSWORD
BASE_URL
DATABASE_URL
BREVO_API_KEY
BREVO_SENDER_EMAIL
PYTHON_VERSION
```
