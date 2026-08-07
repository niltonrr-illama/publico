# Contrato executável de adapters — Espelho Zap Portable

## Contrato adicional 0.3

- O envelope usa `schema_version=3` e inclui
  `conversation_kind=direct|group`, `actor_display_label` e `context_text`.
- A identidade vem, por prioridade, de alias manual, contato/sessão, label do
  evento ou nome público/business; JID e telefone cru nunca viram label.
- `context_text` é contexto interno e nunca é renderizado no Telegram.
- Grupo não aprovado falha antes de persistir corpo, caption ou mídia.
- Recibos aceitos: `messages.update` e `message-receipt.update`, somente por
  avanço monotônico 2→3→4→5.
- OCR/vision não é capability automática; só existe sob pedido humano explícito.

**ID:** CONTRACT-ESPELHO-ZAP-ADAPTER-002
**Status:** ativo e correspondente ao código `0.3.2`
**Fonte canônica:** `src/espelho_zap/models.py`, `src/espelho_zap/adapters/`,
`integrations/openclaw/` e `integrations/hermes/`
**Última revisão:** 2026-08-04

O bridge emite recibos somente para IDs rastreados pelo envio humano. Eventos
sem estado, referência ou evento de provedor permanecem no spool sem ACK.

## 1. Objetivo

OpenClaw, Hermes e um importador legado podem ter objetos e mecanismos de
captura diferentes. Todos terminam na mesma fronteira: um `InboundEvent` v2
validado e enviado à CLI por JSON UTF-8 bounded. O adapter traduz; o core
persiste, deduplica, roteia e entrega.

Este documento descreve somente interfaces que existem no pacote. Não existe
um `poll/materialize/checkpoint` universal fictício: hooks nativos são push;
o adapter JSONL legado é pull e possui cursor próprio.

## 2. Invariantes

Todo adapter:

- aceita somente inbound de WhatsApp comprovado;
- executa antes da decisão do agente/LLM;
- usa uma identidade estável de perfil (`source_profile_id`);
- não escolhe destino Telegram;
- não envia nem responde no WhatsApp;
- não analisa imagem, não faz OCR e não publica transcrição;
- não loga corpo, legenda, contato, telefone, caminho local ou segredo;
- registra sucesso/falha agregada de captura;
- falha visivelmente quando não consegue persistir, sem inventar evento;
- trata mídia somente dentro de raízes explicitamente autorizadas.

## 3. Modelos executáveis

O contrato Python mínimo está em `src/espelho_zap/adapters/base.py`:

```python
@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    adapter_id: str
    platforms: tuple[str, ...]
    capture_stage: str
    supports_media_refs: bool
    supports_partial_records: bool
    requires_explicit_privacy_scope: bool = True
    outbound_whatsapp: bool = False

@runtime_checkable
class InboundAdapter(Protocol):
    @property
    def capabilities(self) -> AdapterCapabilities: ...
```

Adapters Python constroem `RawInboundMessage`/`RawMediaRef` e chamam
`normalize_inbound`. Hooks JavaScript/Python nativos produzem diretamente o
mesmo envelope v2 e chamam:

```text
espelho-zap --config <arquivo-absoluto> ingest -
```

O processo é iniciado com `shell=false`, stdin bounded e stdout/stderr
suprimidos no gateway.

## 4. InboundEvent v2 canônico

Envelope externo aceito pela CLI:

```json
{
  "schema_version": 2,
  "event_id": "raw-or-opaque-message-id",
  "source": "whatsapp",
  "source_profile_id": "stable-runtime-profile",
  "conversation_id": "stable-runtime-conversation",
  "occurred_at": "2026-08-04T00:00:00Z",
  "actor_ref": "stable-runtime-actor",
  "privacy_scope": "owner_private",
  "text": "synthetic fixture",
  "media": []
}
```

Na fronteira da CLI, identificadores crus são convertidos em referências
opacas SHA-256 dentro do perfil. O objeto persistido tem:

| Campo | Regra executável |
| --- | --- |
| `schema_version` | `2`; v1 é lido somente para compatibilidade/hash histórico |
| `event_id` | estável por perfil + conversa + mensagem; replay gera o mesmo ID |
| `source` | `whatsapp` no produto atual |
| `source_profile_id` | referência `profile:<sha256>`; separa contas/perfis |
| `conversation_id` | referência `conversation:<sha256>`; nunca display name |
| `occurred_at` | timestamp com timezone; ausência bloqueia |
| `actor_ref` | referência `actor:<sha256>` |
| `privacy_scope` | `area_shared`, `partnership_restricted` ou `owner_private` |
| `text` | texto original sem placeholder/wrapper automático de mídia |
| `media` | zero ou mais `MediaAttachment`; evento vazio é inválido |

`captured_at` é metadata do ledger, não campo do payload. `source_message_id`,
`kind`, `caption` de topo e `extensions` não fazem parte do schema v2.

## 5. MediaAttachment

```json
{
  "media_id": "media:<sha256>",
  "kind": "image",
  "path": "/private/runtime/file",
  "mime_type": "image/jpeg",
  "sha256": "64-hex-or-empty-before-staging",
  "size_bytes": 1234,
  "caption": "legenda original",
  "managed_temp": false
}
```

- `kind`: `image`, `audio`, `voice`, `video` ou `document`.
- `path` existe somente no armazenamento privado e nunca em diagnóstico.
- O core copia a mídia para spool próprio, calcula/verifica hash e passa a ser
  dono somente da cópia `managed_temp=true`.
- Foto é entregue intacta e somente com legenda original.
- Áudio/voice é entregue intacto; transcrição não aparece no espelho.
- Cópia gerenciada é elegível a purge somente após ACK de entrega; mídia
  incerta/dead permanece para reconciliação, não é apagada cegamente.

## 6. Adapter OpenClaw nativo

Arquivos: `integrations/openclaw/openclaw.plugin.json`, `package.json` e
`dist/index.js`.

- usa os hooks oficiais `inbound_claim`, `message_received`,
  `before_agent_reply`, `message_sending` e `reply_payload_sending`;
- exige `channels.whatsapp.pluginHooks.messageReceived=true`;
- exige consentimento explícito para o hook conversacional não-bundled em
  `plugins.entries.espelho-zap-portable.hooks.allowConversationAccess=true`;
- captura de forma síncrona no `inbound_claim` antes de retornar
  `handled=true`; `message_received` cobre o caminho não vinculado;
- silencia o inbound WhatsApp no `before_agent_reply` e cancela qualquer
  outbound automático residual nos dois hooks de envio;
- com opt-in explícito, o mesmo `message_received` reconhece o fórum Telegram,
  exige operador allowlisted, tópico com rota ativa única e ID de mensagem,
  então carrega `api.runtime.channel.outbound.loadAdapter("whatsapp")` e envia
  texto/mídia pelo canal nativo já pareado, sem LLM ou segundo cliente;
- o fórum exato também é silenciado em `before_agent_reply`; DM, bot, outro
  usuário, outro grupo e tópico ambíguo nunca viram outbound;
- reserva em ledger JSONL privado antes do envio, WIP=1; `reserved` retoma no
  restart, `dispatching` vira `uncertain` e não é reenviado cegamente;
- usa `accountId`/profile configurado para isolamento;
- ignora evento não WhatsApp, outbound, vazio ou staging incompleto;
- remove somente placeholders técnicos documentados de mídia;
- persiste `capture-health.json` 0600 com contagens/códigos;
- relança apenas erro sanitizado, sem path original;
- nunca é colocado em diretório auto-discover no modo prepared-only.

## 7. Adapter Hermes nativo

Arquivos: `integrations/hermes/plugin.yaml` e `integrations/hermes/__init__.py`.

- registra `pre_gateway_dispatch`;
- lê somente o pareamento WhatsApp que já pertence ao Hermes;
- não cria segundo cliente/QR;
- usa apenas biblioteca padrão e CLI absoluta;
- resolve mídia no-follow dentro de raízes declaradas;
- persiste a mesma saúde agregada do adapter OpenClaw;
- como observer, nunca responde nem modifica o inbound;
- outbound humano exige ARM exato ligado ao release, bytes do plugin e
  fingerprint do executável/venv/gateway/loader Hermes do processo atual;
- qualquer alteração desse runtime invalida o ARM anterior e bloqueia antes de
  preparar ou enviar, até novo marker e rearme comprovados.

## 8. Adapter OpenClaw JSONL legado

Arquivo: `src/espelho_zap/adapters/openclaw_jsonl.py`.

É o único adapter pull desta versão. `ingest_file(...)`:

- abre somente arquivo contido na raiz de sessões autorizada;
- recusa trajectory/checkpoint/reset;
- lê apenas linhas completas;
- mantém cursor `(adapter_id, source_ref, generation, byte_offset)` no ledger;
- detecta rotação/truncamento por geração;
- não modifica o JSONL de origem;
- não avança cursor de registro parcial;
- normaliza pelo mesmo `normalize_inbound`.

## 9. Roteamento não pertence ao adapter

Adapter fornece identidade; o ledger exige rota explícita:

```text
conversation:<sha256> -> chat_id negativo + thread_id positivo
```

Sem rota, o evento fica `blocked_no_route`. DM, último chat, nome parecido ou
tópico geral nunca são fallback. Para uma implantação nova:

1. humano cria um supergrupo e ativa Tópicos;
2. adiciona o bot como admin com `can_manage_topics`;
3. executa `route provision-topic ... --confirm-create` para criar tópico e
   gravar a rota exata;
4. se o commit local falhar, o comando tenta remover o tópico criado e relata
   rollback ou estado incerto.

O Bot API não cria o grupo; ele permite criar tópicos no grupo existente.

## 10. Importação legacy runtime

`route import-legacy runtime` reconhece `groupChatId` e `contactTopics`. `topicId`
vira rota exata; IDs recentes/último viram tombstones de dedupe. `topicName` é
somente label e não autoriza rota.

`--source-profile` deve ser o mesmo do adapter ativo. Se a identidade runtime
mudou, `--identity-map` schema 1 associa explicitamente identidade observada à
rota legada. `--dry-run` valida e reverte todas as escritas do banco.

## 11. Saúde e erros

`ESPELHO_ZAP_HOOK_HEALTH_FILE` aponta para JSON privado schema 1:

```json
{
  "schema_version": 1,
  "successes": 1,
  "failures": {},
  "last_success_at": "2026-08-04T00:00:00Z",
  "last_failure_at": "",
  "last_error_code": ""
}
```

`espelho-zap health` combina isso com `PRAGMA quick_check` e estados
agregados. Saúde `failing` bloqueia aceite. O adapter não promete que uma
falha de disco/processo nunca ocorre; promete que ela é sanitizada, persistida
e visível para reparo.

Erros públicos usam apenas códigos como `timestamp_required`,
`media_path_rejected`, `ingest_rejected`, `route_missing` e
`adapter_error`.

## 12. Contract tests obrigatórios

1. inbound WhatsApp aceito; outbound/outro canal ignorado;
2. identidade/perfil estáveis em replay;
3. timestamp e privacy scope fail-closed;
4. mídia contida, no-follow e hash verificado;
5. foto sem `Description:`/OCR e áudio sem transcrição publicada;
6. hook usa stdin bounded e `shell=false`;
7. erros/logs não expõem path/corpo/segredo;
8. saúde agregada persiste sucesso/falha;
9. rota exige supergrupo+tópico e zero DM fallback;
10. JSONL cursor/rotação/linha parcial;
11. imports OpenClaw/Hermes produzem payload v2 equivalente;
12. schema desconhecido falha antes de gravar.

## 13. Compatibilidade

- produto: SemVer;
- evento: v2 ativo, v1 somente leitura compatível;
- banco: migrations `mirror_*`, atualmente schema 6;
- consumer DB: schema próprio 1;
- alterar significado/campo obrigatório do evento exige nova versão;
- mudar hook/API do runtime exige contract tests e canário de carga real.

## 14. Limite de escopo

Este contrato não autoriza por si só canal real ou cutover. Outbound WhatsApp
automático permanece proibido; outbound humano no tópico mapeado segue
`HUMAN_OUTBOUND.md` e exige opt-in e canário. Skill e MCP não são data plane: a skill opera esta CLI; um
MCP read-only pode ser acrescentado no futuro sem mudar o ledger.
