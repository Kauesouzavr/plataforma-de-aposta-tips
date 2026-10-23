# Política de segurança

## Como reportar uma vulnerabilidade

Encontrou uma falha de segurança no **JV Tips**? Por favor, **não abra uma issue pública**.

Use o botão **"Report a vulnerability"** na aba [Security](../../security/advisories/new) deste repositório (o relatório fica privado) ou escreva para **kauesouzavr@gmail.com** com o assunto `[SEGURANÇA] JV Tips`.

Inclua, se puder: o que encontrou, como reproduzir e qual o impacto. Respondo em até **72 horas** e aviso quando a correção for publicada. Quem reportar de forma responsável recebe o crédito, se quiser.

## O que este repositório faz para se proteger

- **Nenhum segredo no código:** chaves e senhas ficam só em variáveis de ambiente; o histórico inteiro é verificado pelo [Gitleaks](https://github.com/gitleaks/gitleaks) a cada push.
- **Análise estática:** o [CodeQL](https://codeql.github.com/) procura vulnerabilidades (injeção, XSS, vazamento de dados) em todo push, pull request e uma vez por semana.
- **Dependências vigiadas:** o Dependabot abre pull requests sozinho quando sai correção de segurança para alguma biblioteca.
- **Senhas com hash** (Werkzeug), **proteção CSRF** em todos os formulários e cookies de sessão `HttpOnly`/`Secure`/`SameSite`.
- **Limite de tentativas** no login, no código de verificação e nos formulários.
- **Pagamento confirmado na API** do Mercado Pago, nunca só pela URL de retorno.
- **Cabeçalhos de segurança** (anti-clickjacking, HSTS, nosniff) em todas as respostas, com teste automatizado.

---

*English: please report vulnerabilities privately through GitHub's "Report a vulnerability" button or by e-mail (kauesouzavr@gmail.com). I reply within 72 hours.*
