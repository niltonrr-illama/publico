# Context Map — Espelho Zap Portable

## Contextos executaveis atuais

- **Canonical event and route model** — `src/espelho_zap/models.py`
  - envelopes inbound imutaveis, referencias opacas, midia, rota e route block;
  - outbound humano fica nas integracoes pre-agent Hermes/OpenClaw e em ledger privado;
    o core nao expoe uma API generica de envio WhatsApp.
- **Ledger and delivery state** — `src/espelho_zap/ledger.py`
  - schema namespaced `mirror_*`, eventos, rotas, tombstones, cursores, outbox, leases e health;
  - `PRAGMA user_version` do banco brownfield e preservado.
- **Policy and routing** — `src/espelho_zap/routing.py`
  - grupo/forum e topico explicitos; falta de rota falha fechada;
  - saneamento de wrapper de imagem e apenas defesa em profundidade.
- **Telegram data plane** — `src/espelho_zap/transport.py`, `src/espelho_zap/telegram.py`, `src/espelho_zap/worker.py`
  - transporte por topico, WIP=1, retry bounded e quarentena `uncertain`;
  - DM nao e destino alternativo.
- **Runtime adapters** — `src/espelho_zap/adapters/`, `integrations/openclaw/`, `integrations/hermes/`
  - OpenClaw JSONL/plugin e Hermes pre-dispatch convergem para o mesmo evento;
  - inbound e passivo; os adapters Hermes e OpenClaw habilitados resolvem
    outbound humano pela rota exata do topico, sem LLM.
- **Legacy import** — `src/espelho_zap/legacy.py`
  - importa rotas, watermark e IDs de dedupe da legacy runtime sem codificar contagens reais;
  - dry-run de importacao continua gate aberto.
- **Independent consumers** — `src/espelho_zap/consumers.py`
  - Daily Notes, claims/evidencias, busca/GBrain reconstruivel e relatorios;
  - cursores e falhas independentes do espelho.
- **Operations and packaging** — `src/espelho_zap/config.py`, `src/espelho_zap/cli.py`, `installer/`, `packaging/`, `skills/`
  - configuracao secret-free, CLI content-safe, instalador transacional, timer opt-in e skill fina.

## Contextos brownfield preservados

- [Capture](./capture/CONTEXT.md) — linguagem historica de captura.
- `capture_v2/` — implementacao/provas anteriores de eventos, cursores, claims e Daily Notes.
- [Mirror](./mirror/CONTEXT.md) — contrato humano WhatsApp ↔ Telegram; `mirror/openclaw/media_policy.py` continua defesa reutilizada.
- [Curation](./curation/CONTEXT.md) — linguagem historica de claims/evidence.
- [Second Brain](./second_brain/CONTEXT.md) — conhecimento revisado; GBrain permanece projecao, nao origem.
- [Migration](./migration/CONTEXT.md) — bundle e readiness anteriores; complementam, nao substituem, o produto instalavel.

## Relacoes

```text
OpenClaw/Hermes adapter
    -> models -> ledger
                 |-> route block (sem rota)
                 |-> route -> outbox -> worker -> Telegram forum topic
                 |-> Daily Notes
                 |-> claims/evidence -> search/GBrain projection
                 `-> aggregate reports

legacy runtime config/state -> legacy importer -> routes + tombstones + watermark
CLI/installer/skill -> operam o produto; nao sao sua fonte de verdade
```

- **Adapter -> Ledger:** normaliza inbound antes da LLM; cursor/rotacao sao locais ao adapter.
- **Ledger -> Mirror:** so uma rota explicita cria uma entrega; adicionar rota depois exige reconciliacao explicita.
- **Ledger -> Consumers:** cada consumidor usa estado proprio e nao compartilha ACK.
- **Claims -> Search/GBrain:** indice e export reconstruiveis; remover o indice nao altera evidencia.
- **Legacy -> Product:** IDs/dedupe/rotas sao importados aditivamente; o brownfield permanece restauravel.
- **DM -> Control plane:** comandos do dono nao se tornam alvo implicito do data plane.
- **Migration -> Runtime:** cutover so depois de staging, single-writer, canario humano e rollback comprovados.

## Desvio consolidado entre design conceitual e layout fisico

O design inicial previa modulos separados como `db.py`, `media.py`, `pipeline.py`, `delivery/telegram.py`, `adapters/hermes.py`, `consumers/daily_notes.py` e `legacy/legacy runtime_import.py`. A implementacao consolidou essas responsabilidades sem mudar as fronteiras:

| Responsabilidade conceitual | Caminho fisico real |
| --- | --- |
| DB/migrations/outbox/leases | `src/espelho_zap/ledger.py` |
| media lifecycle/policy | `src/espelho_zap/models.py`, `routing.py`, `transport.py`, `telegram.py` |
| pipeline/service bounded | `src/espelho_zap/worker.py` |
| Telegram delivery | `src/espelho_zap/telegram.py` |
| OpenClaw adapter | `src/espelho_zap/adapters/openclaw_jsonl.py` + `integrations/openclaw/` |
| Hermes adapter | `integrations/hermes/__init__.py` |
| legacy runtime import | `src/espelho_zap/legacy.py` |
| Daily Notes/claims/search/reports | `src/espelho_zap/consumers.py` |
| install/uninstall/systemd | `installer/install.sh` + `packaging/systemd/` |

Documentos operacionais devem apontar para esses paths reais. Os nomes conceituais continuam validos apenas para explicar responsabilidades, nao como arquivos/comandos existentes.

## Fronteira de aceite

Testes locais comprovam o candidato, mas nao comprovam:

- instalacao Linux completa;
- adapters conectados aos runtimes reais;
- topicos Telegram reais;
- single-writer/cutover;
- canario humano de texto/foto/audio;
- publicacao comunitaria sob licenca aprovada.

Esses gates permanecem em `docs/ACCEPTANCE_TESTS.md` e `.specs/project/STATE.md`.
