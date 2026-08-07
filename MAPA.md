# MAPA — Espelho Zap Portable

## Identidade e estado

- ID: `PRJ-WHATSAPP-COCKPIT-PORTABLE`
- Distribuicao: `espelho-zap-portable`
- Pacote Python: `espelho_zap`
- CLI: `espelho-zap`
- Skill operacional: `espelho-zap-portable`
- Dono: Operator
- Dono tecnico: Maintainer
- Origem brownfield: Divina/legacy runtime/OpenClaw, preservada para importacao e rollback
- Runtimes suportados por adapter: OpenClaw e Hermes
- Estado: candidato portatil implementado e testado localmente; instalacao Linux, canal real, cutover e canario humano ainda nao aceitos
- Dados reais: fora do pacote/Git; somente fixtures sinteticas e metadados agregados podem compor o artefato comunitario

## Produto executavel

| Caminho real | Funcao | Evidencia/estado |
| --- | --- | --- |
| `pyproject.toml` | metadata, pacote e console script | implementado; build limpo de wheel/sdist ainda e gate separado |
| `src/espelho_zap/models.py` | envelopes imutaveis, midia, rota e bloqueios | coberto por `tests_core/` |
| `src/espelho_zap/ledger.py` | schema `mirror_*` v9, eventos, admissão de grupos, identidades, rotas, deliveries, recibos, canários, leases e cursores | coberto por `tests_core/`; coexiste com Captura V2 sem tomar `PRAGMA user_version` |
| `src/espelho_zap/routing.py` | politica fail-closed, video e saneamento defensivo de legenda | coberto por `tests_core/` e `mirror/tests/` |
| `src/espelho_zap/transport.py` | contrato de transporte, idempotencia local, validacao/purge governado de midia | coberto por `tests_core/` |
| `src/espelho_zap/telegram.py` | Bot API com `chat_id` de grupo e `message_thread_id` obrigatorios | fake transport/requests cobertos; canal real pendente |
| `src/espelho_zap/worker.py` | worker bounded, WIP=1, retry seguro e `uncertain` | coberto por `tests_core/` |
| `src/espelho_zap/adapters/base.py` | contrato canonico de adapter inbound-only | contract tests locais |
| `src/espelho_zap/adapters/openclaw_jsonl.py` | tail JSONL OpenClaw, cursor/rotacao/midia | contract tests locais |
| `integrations/openclaw/` | plugin `message_received` que alimenta a CLI sem shell | contrato estatico/local; instalacao OpenClaw real pendente |
| `integrations/hermes/` | plugin `pre_gateway_dispatch` pre-agente | contrato local; instalacao Hermes real pendente |
| `src/espelho_zap/legacy.py` | importador aditivo de rotas/dedupe/watermark legacy runtime | import/replay e dry-run imutavel/read-only cobertos |
| `src/espelho_zap/consumers.py` | Daily Notes, claims/evidencias, export de busca e relatorios | coberto por `tests_consumers/`; ativacao produtiva separada |
| `src/espelho_zap/config.py` | TOML secret-free, env/secret file e limites | coberto por `tests_packaging/` |
| `src/espelho_zap/cli.py` | init, doctor, health, backup, rotas, import, ingest e worker | coberto localmente; comandos inexistentes nao devem ser documentados |
| `installer/install.sh` | install/upgrade/preflight/uninstall transacional por usuario | contrato/testes locais; smoke Linux real pendente |
| `packaging/systemd/` | oneshot parametrizado + timer desabilitado por padrao | artefato presente; ativacao real pendente |
| `skills/espelho-zap-portable/` | operacao por CLI/docs, sem daemon ou memoria embutida | artefato e metadata testados; descoberta no runtime alvo pendente |

## Brownfield preservado

| Caminho | Papel atual |
| --- | --- |
| `capture_v2/` | referencia executavel da Captura V2 e baseline de 18 testes; nao e o novo entrypoint |
| `mirror/` | politica OpenClaw de imagem e baseline de 6 testes; defesa reutilizada |
| `migration/` | bundle/manifest/readiness legado e baseline de 5 testes; nao substitui o instalador |
| `capture/`, `curation/`, `second_brain/` | contextos e linguagem historicos; o codigo graduado esta em `src/espelho_zap/` |

## Testes e especificacao

| Caminho | Cobertura |
| --- | --- |
| `tests_core/` | modelos, ledger, rotas, outbox, worker, Telegram e adapters |
| `tests_consumers/` | Daily Notes, claims, busca e relatorios |
| `tests_packaging/` | config, CLI, backup, destino Telegram, installer, units e skill |
| `.specs/features/portable-product/` | requisitos, design e tarefas rastreaveis |
| `docs/` | PRD, arquitetura, adapters, ameacas, instalacao e aceite |

## Fluxo e fronteiras

```text
WhatsApp pareado no host
  -> adapter pre-agente
  -> envelope canonico
  -> ledger SQLite imutavel
       -> rota explicita -> outbox -> topico de forum Telegram
       -> Daily Notes
       -> claims/evidencias -> busca/GBrain substituivel
       -> relatorios agregados

DM do agente = plano de controle; nunca destino/fallback do espelho.
```

- Captura nao depende de Telegram, LLM, Daily Notes, claims ou GBrain.
- Cada consumidor tem cursor/ACK proprio; falha de um nao avanca os demais.
- Ausencia de rota vira `blocked_no_route` persistente e exige `route reconcile` explicito.
- A entrega Telegram exige supergrupo/forum e topico; destino privado e recusado.
- Midia original nao e substituida por descricao/OCR/vision e so pode ser removida sob politica governada apos entrega confirmada.
- O produto possui lane de outbound humano no topico mapeado; interfaces
  genericas, automaticas e de LLM permanecem bloqueadas.
- Contatos diretos podem autocriar topico; grupos usam allowlist exata e nunca
  autocriam topico. Grupo aprovado permanece `agent_mode=none` por padrao.
- `policy.py` concentra grill de dez campos, identidade humana segura, recibos
  monotonicos e matriz de canarios humanos; esses contratos nao dependem do
  texto de um prompt de runtime.

## Evidencia automatica atual

Execucao local em 2026-08-04:

- 29/29 testes brownfield PASS;
- 79 PASS + 2 SKIP POSIX/Linux em `tests_core/`;
- 11/11 PASS em `tests_consumers/`;
- 18 PASS + 1 SKIP POSIX/Linux em `tests_packaging/`.

Total: 137 PASS, 3 SKIP dependentes de POSIX/Linux. Isso comprova o candidato local, nao a instalacao nem o canal real.

## Gates ainda abertos

1. wheel/sdist e instalacao em ambiente Linux limpo;
2. validacao POSIX de modos e sintaxe/execucao do instalador;
3. instalacao e contract probe no Hermes/OpenClaw alvo;
4. validacao do supergrupo/forum e de cada topico real;
5. prova single-writer antes de ativar o destino;
6. canario humano de texto, foto e audio no topico exato, uma unica vez e sem `Description:`;
7. replay/restart e rollback comprovados;
8. publicacao/handoff por commit exato.

## O que nao e permitido

- usar compactacao de sessao como memoria;
- considerar GBrain/embeddings a fonte canonica;
- apagar legado porque existe resumo ou indice;
- inferir rota por nome, telefone, titulo, ultimo chat ou DM;
- ativar outbound automatico ou outbound humano sem allowlist/rota/canario;
- rodar dois writers para o mesmo perfil;
- declarar aceite produtivo com base apenas em testes locais;
- documentar comandos ou paths conceituais que nao existam no pacote.

## Roteamento documental

- Entrada: `README.md`
- Contextos: `CONTEXT-MAP.md`
- Estado: deployment-specific state (not included)
- Requisitos/design/tarefas: the product documents in this repository
- Instalacao: `docs/INSTALLATION.md`
- Aceite: `docs/ACCEPTANCE_TESTS.md`
- Migracao brownfield: `docs/MIGRATION_RUNBOOK.md`
- Memoria: deployment-specific memory (not included)
- Incidentes/handoff: the product release history in this repository
