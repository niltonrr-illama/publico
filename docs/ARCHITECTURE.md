# Arquitetura executável — Espelho Zap Portable

## Resultado

O produto captura uma mensagem inbound do WhatsApp **antes da LLM**, registra
um evento imutável e entrega uma única cópia no tópico exato de um supergrupo
Telegram. O plano de dados não depende do agente para decidir o destino. A DM
do operador é plano de controle e nunca é fallback de entrega.

```text
WhatsApp já pareado no host
  -> hook passivo do host (sem resposta automática)
  -> InboundEvent v2 + mídia staged
  -> SQLite privado e versionado
  -> worker WIP=1, perfil isolado
  -> Telegram supergrupo + tópico explícito
  -> ACK -> limpeza da mídia gerenciada
  -> consumidores opcionais: Daily Notes, claims, busca e relatórios

Operador allowlisted no tópico Telegram
  -> hook pre-agent (sem LLM)
  -> intent/outbox durável por message_id
  -> dispatcher WIP=1
  -> endpoint loopback autenticado
  -> mesma sessão WhatsApp pareada
```

O bot do Telegram não cria o grupo. O operador cria uma vez um supergrupo com
Tópicos ativados e concede ao bot `can_manage_topics`. O comando
`route provision-topic` pode então criar um tópico e persistir a rota exata.

## Componentes

| Componente | Responsabilidade | Limite |
|---|---|---|
| `integrations/hermes` | capturar WhatsApp e outbound humano do fórum no `pre_gateway_dispatch`, sempre antes da LLM | somente endpoint loopback dedicado; nunca entrega o plano de dados à LLM |
| `integrations/openclaw` | observar/capturar inbound e silenciar/cancelar outbound WhatsApp | ativação bloqueada sem hooks passivos carregados e comprovados |
| `adapters/openclaw_jsonl.py` | importação incremental da fonte JSONL legada | leitura `mode=ro`/cursor; não é o caminho nativo novo |
| `media.py` | staging atômico, hash, containment, quota e órfãos | só raízes aprovadas; nunca mídia arbitrária do host |
| `ledger.py` | eventos, rotas, leases, dedupe, reconciliação e auditoria | SQLite privado; tabelas prefixadas; não usa `PRAGMA user_version` |
| `worker.py` | WIP=1, lease renovável, retry limitado e cleanup | só reivindica o próprio `source_profile_id` |
| `telegram.py` | Bot API com streaming e tópico obrigatório | nenhum fallback para DM; resposta incerta vira quarentena |
| `consumers.py` | projeções opcionais de segundo cérebro | escopos explícitos e cursores independentes |
| `installer/install.sh` | prepared-only, instalação, upgrade, rollback e uninstall | mutações externas somente por opt-in comprovado |

## Identidade e isolamento

`source_profile_id`, `conversation_id`, `actor_ref` e `event_id` entram no
ledger como referências opacas. `conversation_id` deriva do perfil e da
conversa de origem. Nome e telefone podem aparecer como título humano do tópico,
mas não são chave de roteamento.

Cada evento persiste seu `source_profile_id`. `claim_next()` filtra por esse
campo e o worker rejeita ingestão de outro perfil. Assim um token Telegram
associado ao perfil A não pode retirar uma mensagem capturada pelo perfil B,
mesmo se ambos compartilharem fisicamente o SQLite.

## Contrato de rota

Uma rota ativa contém:

```text
conversation_id -> chat_id negativo de supergrupo + thread_id positivo
```

- `chat_id >= 0` é rejeitado por modelo, CLI e trigger SQLite;
- `thread_id <= 0` é rejeitado;
- ausência de rota captura o evento, mas cria `blocked_no_route`;
- mudança de rota bloqueia entregas antigas como `route_changed`;
- liberar ou rebindar exige ação explícita e evidência auditada;
- nunca usar a DM do operador como destino implícito.

## Ledger schema 7

Tabelas do plano de entrega:

- `mirror_schema_migrations`;
- `mirror_events`;
- `mirror_routes`;
- `mirror_deliveries`;
- `mirror_delivery_attempts`;
- `mirror_leases`;
- `mirror_runtime_locks`;
- `mirror_legacy_delivered`;
- `mirror_legacy_imports`;
- `mirror_source_cursors`;
- `mirror_route_blocks`;
- `mirror_managed_media`;
- `mirror_media_purge_audit`;
- `mirror_conversation_policies` e `mirror_conversation_policy_audit`;
- `mirror_conversation_aliases` e `mirror_conversation_alias_audit`;
- `mirror_delivery_reconciliation_audit`.

Tabelas dos consumidores:

- `mirror_consumer_schema_migrations`;
- `mirror_consumer_event_index`;
- `mirror_consumer_cursors`;
- `mirror_daily_note_events`;
- `mirror_claims`, `mirror_claim_evidence` e `mirror_claim_supersessions`.

Estados de entrega: `pending`, `inflight`, `retry`, `sent`, `dead`, `blocked` e
`uncertain`. Bloqueios de rota ficam separadamente como `blocked_no_route` ou
`requeued`.

Eventos são append-only: triggers rejeitam UPDATE e DELETE. Entrega, lease,
cursores e projeções são estado operacional mutável e auditável.

## Idempotência e resultado incerto

1. `event_id + payload_hash` impede colisão silenciosa;
2. uma entrega lógica por evento impede reenqueue;
3. WIP=1 global no ledger evita envios concorrentes;
4. lease expirado vira `uncertain`, não retry automático;
5. qualquer falha após um POST Telegram sem prova de rejeição vira
   `uncertain`;
6. `uncertain` só vira `sent` ou `retry` por reconciliação explícita com hash
   de evidência;
7. replay de evento já enviado não restaura a mídia apagada.

Isso é fail-safe e recuperável; não é uma promessa impossível de “zero falha”.

## Mídia e uso de disco

- foto: arquivo intacto, somente legenda original, sem `Description:`, OCR ou
  visão automática;
- áudio/voz: arquivo intacto e reproduzível, sem transcrição visível;
- streaming para Telegram, sem carregar o arquivo inteiro em RAM;
- staging somente em raiz gerenciada, com SHA-256, modo privado e publicação
  atômica;
- quota `maximum_spool_bytes` serializada pela transação de ingestão;
- depois de `sent`, mídia vira `cleanup_pending` e é removida;
- mídia em `retry` ou `uncertain` é preservada;
- mídia `dead`, `blocked` ou `blocked_no_route` só é apagada após
  `media authorize-purge --evidence-ref ...`;
- `media report` expõe somente contagens e bytes, nunca caminhos ou conteúdo.

## Segundo cérebro

O ledger é multicanal e independente do GBrain. Os consumidores são projeções:

- `consumer daily-notes`: projeção bounded por escopo e cursor;
- `consumer claim-add`: claim imutável com evidências e supersessão;
- `consumer search-export`: JSONL determinístico e provider-neutral;
- `consumer report`: agregados ou pendências sem conteúdo.

GBrain é opcional: pode consumir a projeção de claims, mas não é requisito para
captura, espelho, Daily Notes ou busca nativa do Hermes.

## Modos de distribuição

O core é um pacote Python Apache-2.0. A skill ensina operação; os plugins são
adaptadores finos; o instalador controla lifecycle. MCP não é o plano de dados e
fica opcional para futuras integrações de consulta. O pacote fonte exclui os
diretórios brownfield da legacy runtime; eles permanecem apenas no repositório como
referência histórica.

## Gates de produção

Nenhuma ativação real é aceita sem:

1. pacote/manifesto verificados;
2. plugin descoberto e hooks passivos registrados;
3. prova de que inbound WhatsApp não chega à LLM, auto-outbound é cancelado e
   apenas o outbound humano allowlisted alcança o endpoint dedicado;
4. destino Telegram verificado como supergrupo forum;
5. uma conversa real roteada para um tópico e canário humano;
6. foto uma vez, sem descrição; áudio e texto uma vez;
7. restart/replay sem duplicata;
8. backup e rollback testados;
9. single-writer confirmado.
