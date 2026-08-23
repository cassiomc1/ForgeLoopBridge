# ForgeLoopBridge — Historical Audit & Improvements Log (v2.0)

> **ARCHIVE / HISTORICAL RECORD:** All items listed in this document were successfully implemented in ForgeLoopBridge v2.0 (security hardening, rate limiting, DOMPurify sanitization, SQLite WAL mode, SSE streaming, authentication, test suite, and CI).
>
> Active project alignment and roadmap follow the **ForgeLoop 1.5.0 Integration Specification** documented in [README.md](README.md) and [examples/AUTONOMY.md](examples/AUTONOMY.md).


---

## 🔴 Crítico (segurança)

### 1. XSS no frontend — Markdown sem sanitização
**Onde:** `static/index.html:302`
```js
<div class="message-body">${marked.parse(msg.content)}</div>
```
`marked.parse()` não sanitiza HTML. Qualquer mensagem com `<script>` ou `<img onerror=...>` executa no navegador de quem visualiza o board.

**Correção:** adicionar [DOMPurify](https://github.com/cure53/DOMPurify):
```js
const clean = DOMPurify.sanitize(marked.parse(msg.content));
```

### 2. Tokens padrão hardcoded e aceitos silenciosamente
**Onde:** `main.py:19-20`
```python
ENGINEER_TOKEN = os.getenv("ENGINEER_TOKEN", "engineer_secret")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "worker_secret")
```
Se ninguém definir as variáveis de ambiente, qualquer pessoa que leia o README tem acesso total.

**Correção:** falhar na inicialização (ou logar warning forte) se os tokens não forem definidos explicitamente:
```python
ENGINEER_TOKEN = os.getenv("ENGINEER_TOKEN")
WORKER_TOKEN = os.getenv("WORKER_TOKEN")
if not ENGINEER_TOKEN or not WORKER_TOKEN:
    raise RuntimeError("Defina ENGINEER_TOKEN e WORKER_TOKEN no ambiente")
```

### 3. Comparação de tokens vulnerável a timing attack
**Onde:** `main.py:92-95`
```python
if msg.token == ENGINEER_TOKEN:
```
**Correção:** usar comparação em tempo constante:
```python
import secrets
if secrets.compare_digest(msg.token, ENGINEER_TOKEN):
```

### 4. Leitura da API sem autenticação
**Onde:** `main.py:71-86`
`GET /api/messages` é público — qualquer um pode ler toda a conversa entre Engineer e Worker (que pode conter informações sensíveis do projeto).

**Correção:** exigir um dos dois tokens via header (`Authorization: Bearer <token>` ou query param) para ler mensagens.

### 5. Sem rate limiting
**Onde:** `POST /api/messages`
Um agente com loop bugado (ou atacante) pode inundar o banco infinitamente.

**Correção:** usar `slowapi` ou um limite simples em memória por IP/token (ex.: N msgs/minuto).

---

## 🟠 Importante (bugs e robustez)

### 6. Ordenação/filtro por timestamp flutuante (`since`) — risco de perder mensagens
**Onde:** `main.py:76-84` e `static/index.html:313`
`time.time()` tem resolução limitada e relógios podem sofrer ajustes. Duas mensagens no mesmo instante, ou `created_at == since`, podem ser perdidas pelo filtro `created_at > ?`.

**Correção:** paginação baseada em `id` auto-incremental:
```
GET /api/messages?after_id=42
SELECT ... WHERE id > ? ORDER BY id ASC
```
Manter `since` como alias deprecated para compatibilidade, mas migrar clientes para `after_id`.

### 7. Sem paginação/limite em `GET /api/messages`
Com o tempo o board cresce indefinidamente e o primeiro load fica pesado.

**Correção:** adicionar `limit` (default ~200) + ordenação DESC no load inicial, e `before_id` para scroll histórico no frontend.

### 8. Conexão SQLite aberta por request + sem WAL
**Onde:** `main.py:74, 105, 119`
Múltiplos writes concorrentes podem causar `database is locked`. Abrir conexão a cada request também é desperdício.

**Correção:**
- Habilitar WAL no `init_db()`: `PRAGMA journal_mode=WAL;`
- Usar um pool/conn compartilhado (ou `busy_timeout=5000`) para reduzir contenção.

### 9. `reload=True` no entrypoint de produção
**Onde:** `main.py:148`
```python
uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
```
Rodar `python main.py` em produção liga hot-reload (consumo extra e risco de segurança).

**Correção:** controlar por env var:
```python
uvicorn.run("main:app", host=HOST, port=PORT, reload=os.getenv("RELOAD") == "1")
```

### 10. Dockerfile ignora `HOST`/`PORT` e roda como root
**Onde:** `Dockerfile:14`
```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```
As env vars `HOST`/`PORT` documentadas são ignoradas no container, e o processo roda como root.

**Correção:**
```dockerfile
RUN useradd -m appuser && chown -R appuser /app
USER appuser
CMD ["sh", "-c", "uvicorn main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
```
Aproveitar e adicionar `HEALTHCHECK` usando `/api/status`.

### 11. Caminhos relativos ao CWD
**Onde:** `main.py:18, 139, 143`
`DB_PATH = "data/forgebridge.db"` e `FileResponse("static/index.html")` quebram se o processo iniciar fora da raiz do projeto.

**Correção:** ancorar no diretório do arquivo:
```python
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("FORGEBRIDGE_DB", BASE_DIR / "data" / "forgebridge.db"))
STATIC_DIR = BASE_DIR / "static"
```

### 12. Dependência não utilizada
**Onde:** `requirements.txt:5`
`python-multipart` só é necessário se houver upload de formulários — não há nenhum endpoint que use.

**Correção:** remover, ou justificar mantê-la.

### 13. CDN sem integridade (supply chain)
**Onde:** `static/index.html:7`
```html
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
```
Sem `integrity`/SRI e sem versão pinada. Se o CDN for comprometido, código arbitrário roda no board.

**Correção:** fixar versão + SRI:
```html
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"
        integrity="sha384-..." crossorigin="anonymous"></script>
```
(idealmente vendorar o JS localmente junto com DOMPurify).

### 14. `cursor.lastrowid` pode ser `None`
**Onde:** `main.py:111`
Tipagem de `aiosqlite` permite `Optional[int]`; passar direto para `MessageOut(id=msg_id)` funciona na prática, mas vale um assert/guard explícito.

### 15. Sem tratamento de erro global nem logging
Nenhum log de requests, erros de banco, ou tentativas de autenticação inválidas.

**Correção:** configurar `logging` básico, registrar tentativas de token inválido (útil para detectar agente mal configurado), e considerar middleware de exceções.

---

## 🟡 Melhorias (UX e qualidade)

### 16. Frontend: seletor de papel (role) é enganoso
**Onde:** `static/index.html:260-263`
O `<select>` Engineer/Worker sugere que escolhe o papel, mas o papel real vem do **token** validado no servidor. Usuário pode selecionar "Engineer" com token de worker e ficar confuso com o resultado.

**Correção:** inferir/desabilitar o select após validar o token (ex.: botão "conectar" que faz um `GET` autenticado e mostra qual papel o token representa), ou remover o select e mostrar o papel retornado na resposta.

### 17. Frontend: auto-scroll forçado a cada poll
**Onde:** `static/index.html:308`
`window.scrollTo(...)` roda sempre que chega mensagem — interrompe leitura quando o usuário rolou para cima.

**Correção:** só rolar se o usuário já estiver próximo do fim da página (~100px).

### 18. Frontend: hint expõe tokens padrão na UI
**Onde:** `static/index.html:268`
Em produção, exibir `engineer_secret / worker_secret` na tela anula qualquer troca de token feita via env var.

**Correção:** remover a linha ou buscar um endpoint `/api/meta` público que diga apenas se há tokens customizados (sem revelá-los).

### 19. Frontend: polling fixo de 8s + request duplicado de status
Cada ciclo faz 2 requests (`/api/messages` e `/api/status`). Com muitos observadores isso multiplica carga desnecessariamente.

**Correção:** incluir contadores no próprio payload de `/api/messages`, e/ou usar **Server-Sent Events (SSE)** ou **WebSocket** para push em tempo real (FastAPI suporta nativamente) — elimina polling completamente.

### 20. `examples/worker_poll.py`: estado perdido no restart e spam de status
- `last_seen = 0.0` no restart reprocessa todas as mensagens antigas como "novas".
- Posta automaticamente uma mensagem placeholder ("processing...") a cada instrução — polui o board em execuções repetidas.

**Correção:** persistir `last_seen` em arquivo (`.last_seen`); tornar o post automático opcional via flag/env.

### 21. Sem testes
Zero cobertura. Endpoints simples, fáceis de testar com `pytest` + `httpx.AsyncClient` + banco temporário.

**Correção:** adicionar testes mínimos: auth válida/inválida, post/get, filtro `since`/`after_id`, validação de content vazio.

### 22. Sem CI
Adicionar GitHub Actions rodando lint (`ruff`) + testes em cada PR.

### 23. Funcionalidades que agregariam valor
- **DELETE /api/messages/{id}** (com token do próprio autor) para corrigir mensagens enviadas por engano.
- **Reações/acks**: worker marcar tarefa como "acknowledged" sem precisar postar mensagem nova (tabela separada ou campo `status`).
- **Tópicos/threads**: campo opcional `topic` na mensagem para paralelizar múltiplas tarefas no mesmo board.
- **Retenção/pruning**: comando ou job para arquivar mensagens antigas e manter o banco enxuto.
- **Backup do SQLite**: documentar estratégia (o `.gitignore` já exclui `data/*.db` — bom).
- **docker-compose.yml** para subir com um comando já configurando tokens via `.env`.

### 24. Documentação
- README diz "Auto-refresh every 8 seconds" — ok, mas documentar que a leitura da API é pública (hoje ninguém espera isso).
- Adicionar seção de **segurança/deploy** recomendando: tokens fortes (`openssl rand -hex 32`), HTTPS via reverse proxy, e não expor porta diretamente.

---

## Resumo priorizado

| # | Item | Severidade | Esforço |
|---|------|-----------|---------|
| 1 | Sanitizar Markdown (XSS) | 🔴 Crítica | Baixo |
| 2 | Exigir tokens via env | 🔴 Crítica | Baixo |
| 4 | Autenticar leitura da API | 🔴 Crítica | Médio |
| 5 | Rate limiting | 🔴 Alta | Baixo |
| 6 | Paginação por `id` | 🟠 Alta | Médio |
| 8 | WAL + conexão compartilhada | 🟠 Média | Baixo |
| 9-10 | Reload/Docker hardening | 🟠 Média | Baixo |
| 11 | Paths absolutos | 🟠 Média | Baixo |
| 13 | SRI/vendor de JS | 🟠 Média | Baixo |
| 16-20 | UX frontend + exemplo | 🟡 Média | Médio |
| 21-22 | Testes + CI | 🟡 Média | Médio |
| 19 | SSE/WebSocket | 🟡 Baixa | Alto |
