# Espelho Zap Portable

**ID:** PRJ-WHATSAPP-COCKPIT-PORTABLE
**Tipo:** produto Python instalável + contratos de integração
**Status:** candidato `0.3.4`; aceite humano bidirecional ainda pendente
**Dono:** Operator
**Dono técnico:** Maintainer / coding assistant
**Escopo:** captura WhatsApp, espelho Telegram, ledger, projeções e migração
**Fonte canônica:** sim, para o produto portátil
**Fonte superior:** deployment-specific backlog (not included)
**Relacionados:** the product release history in this repository
**Sensibilidade:** código L1; dados reais permanecem fora do Git
**Última revisão:** 2026-08-06

## Resultado

O Espelho Zap deixou de ser apenas um script dentro da legacy runtime e passou a ter
um produto independente de runtime:

- distribuição Python: `espelho-zap-portable`;
- pacote importável: `espelho_zap`;
- CLI: `espelho-zap`;
- serviço/timer systemd por usuário;
- plugin fino para Hermes;
- plugin fino para OpenClaw;
- skill operacional `espelho-zap-portable`;
- PRD, spec, arquitetura, threat model, instalação, testes e rollback;
- importador aditivo do mapa e do estado de deduplicação da legacy runtime.

A skill não contém a implementação. O MCP não é necessário. O serviço Python
é o dono da lógica determinística e do estado durável; os runtimes apenas
entregam eventos normalizados.

## Arquitetura

```text
WhatsApp já pareado no host
        |
        v inbound
adaptador pre-agent (Hermes ou OpenClaw)
        |
        v
ledger SQLite imutável + rotas + outbox + dedupe
        |----------------------|----------------------|
        v                      v                      v
Telegram forum topic     Daily Notes/relatórios   claims/busca/GBrain
(plano de dados)         (projeções)              (consumidores opcionais)

Telegram DM = plano de controle; nunca destino de conteúdo espelhado

operador allowlisted no tópico Telegram
        |
        v outbound humano, WIP=1, sem LLM
mesma conversa WhatsApp da rota exata do tópico
```

Captura, entrega e consumidores têm falhas independentes. Uma falha no
Telegram não apaga o evento. Uma falha no GBrain não interrompe a captura.
Uma rota desconhecida preserva o evento e bloqueia a entrega.

## Invariantes executáveis

1. Uma rota de dados contém um `chat_id` negativo de grupo/supergrupo e um
   `message_thread_id` positivo.
2. Não existe fallback para DM, nome parecido, telefone, título ou primeiro
   tópico disponível.
3. Rota ausente/desativada produz `blocked_no_route`, zero chamada Telegram e
   exige reconciliação explícita após o operador cadastrar a rota.
4. Evento repetido com o mesmo payload é replay; mesmo ID com payload diferente
   é conflito.
5. Entrega com resultado remoto ambíguo vira `uncertain` e não é reenviada
   cegamente.
6. Foto conserva bytes e apenas a legenda original; `Description:`, OCR e
   vision automáticos não entram no espelho. OCR/visão só podem ocorrer por
   solicitação explícita do operador, fora do caminho de transporte e sem
   substituir o arquivo original.
7. Voice note usa `sendVoice`; áudio genérico usa `sendAudio`.
   A transcrição, quando habilitada internamente, alimenta somente o contexto
   privado; nunca aparece no Telegram.
8. Mídia temporária só é removida depois do ACK e somente dentro da raiz
   gerenciada explicitamente; a retenção máxima é 48 horas, com purge
   determinístico dos itens mais antigos após ACK.
9. O produto possui outbound WhatsApp somente para mensagens humanas de
   operadores allowlisted no tópico mapeado; não possui auto-reply, outbound de
   LLM nem destino escolhido por texto/comando. Para grupos, o tópico mapeado
   não basta: a mesma conversa exata também precisa estar `group_approved` no
   ledger do perfil; ausência ou divergência bloqueia o envio.
10. Apenas um worker de entrega possui a autoridade do perfil; o ledger e os
    cursores sobrevivem a reinícios.
11. O operador cria uma única vez o supergrupo Telegram com Topics e concede ao
    bot `can_manage_topics`; o pacote cria, sob confirmação explícita, cada
    tópico necessário e persiste o `chat_id` + `thread_id` exatos. Nunca cria ou
    usa DM como fallback.
12. "Uma conversa por tópico" vale para cada conversa WhatsApp selecionada —
    tanto contato individual quanto grupo. O pacote não importa toda a agenda
  nem cria tópicos para contatos que nunca foram selecionados.
13. Recibos WhatsApp são transportados como eventos do provedor, nunca
    inferidos: `2=sent/device`, `3=delivered`, `4=read`, `5=played`. O ledger
    só avança e a ausência de recibo mantém o último estado conhecido.

“Sem falhas” neste produto significa falhar de forma segura, observável e
recuperável. Nenhum software pode impedir indisponibilidade de rede ou de um
provedor externo.

## Instalação rápida em Linux

O instalador é por usuário, transacional e deixa o timer desabilitado:

```bash
cd projects/whatsapp_cockpit_portable
MIRROR_MEDIA_ROOT=/caminho/absoluto/para/midia-aprovada
# Se o runtime guardar anexos de saída em outro diretório, ele também deve ser
# informado explicitamente. Nunca use uma raiz ampla como /tmp ou todo o cache.
RUNTIME_MEDIA_ROOT=/caminho/absoluto/para/cache-do-runtime/images
bash installer/install.sh preflight --source . --runtime hermes \
  --media-root "$MIRROR_MEDIA_ROOT" --media-root "$RUNTIME_MEDIA_ROOT"
bash installer/install.sh install --source . --runtime hermes \
  --media-root "$MIRROR_MEDIA_ROOT" --media-root "$RUNTIME_MEDIA_ROOT"
```

### Raízes de mídia são bidirecionais e explícitas

As mesmas raízes aprovadas são usadas para validar os dois sentidos do
transporte: WhatsApp → Telegram e Telegram → WhatsApp. Isso não significa
"liberar todas as mídias do servidor": cada caminho precisa ser cadastrado
explicitamente, existir, ser diretório regular e ficar sob controle do
operador. Em uma instalação Hermes, o diretório de anexos do adaptador
(`cache/images`, quando aplicável) deve ser passado como uma segunda opção
`--media-root`; o produto não o descobre automaticamente. Sem essa segunda
raiz, texto continua funcionando, mas uma foto outbound pode ser rejeitada
com `media_path_rejected` antes do bridge.

O instalador grava a lista em `[worker].source_media_roots`, preserva-a em
restarts e permite revogação somente com `--clear-media-roots`. A retenção
temporária continua limitada a 48 horas após ACK.

Esse comando prepara o plugin, mas não o habilita. Troque o alvo por
`--runtime openclaw --runtime-home "$HOME/.openclaw"` para OpenClaw;
`--runtime none` continua sendo o default. O modo prepared-only grava o plugin
fora de qualquer raiz de discovery, persiste `worker.profile_id` e um template
sem segredos, sem criar/tocar o home, a configuração ou o gateway do runtime.
Em uma configuração existente, omitir `--media-root` preserva exatamente as
raízes autorizadas; numa instalação limpa a lista nasce vazia/fail-closed. Use
`--clear-media-roots` somente para revogar todas as raízes explicitamente.

Depois de revisar o plugin e o dry-run, o opt-in OpenClaw `--enable-runtime`
persiste o ambiente, usa o instalador oficial do runtime, reinicia o gateway e
exige prova de carregamento dos hooks passivos. Ele também habilita
`channels.whatsapp.pluginHooks.messageReceived` e o consentimento explícito
`hooks.allowConversationAccess=true`; não cria nem pareia um segundo WhatsApp.
Hermes fica preparado: como o CLI atual não prova que o hook foi carregado no
gateway em execução, o instalador recusa a ativação automatizada até um canário
humano separado e autorizado. Em seguida:

Durante uma ativação Hermes que possa reiniciar o gateway, o instalador executa
`hermes config validate` antes do restart. Uma configuração YAML inválida aborta
a transação e restaura a instalação anterior; o runtime não deve cair em uma
configuração de fallback silenciosa.

```bash
ESPELHO_ZAP_BIN="${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/venv/bin/espelho-zap"
"$ESPELHO_ZAP_BIN" --config ~/.config/espelho-zap/config.toml doctor --allow-missing-token
"$ESPELHO_ZAP_BIN" --help
```

Leia `docs/INSTALLATION.md` antes de importar estado ou ativar o timer.

## Operação mínima

Todos os comandos emitem JSON sanitizado por padrão:

```bash
# importar o shape real groupChatId/contactTopics da legacy runtime
espelho-zap --config <CONFIG> route import-legacy runtime <ILLAMA_CONFIG_JSON>

# inspecionar metadados opacos e saúde
espelho-zap --config <CONFIG> route list
espelho-zap --config <CONFIG> route blocked-list
espelho-zap --config <CONFIG> health

# criar backup SQLite verificado em um caminho novo
espelho-zap --config <CONFIG> backup <NOVO_BACKUP.sqlite3>

# validar o fórum sem enviar mensagem e cadastrar a rota
espelho-zap --config <CONFIG> route verify-destination <CHAT_ID_NEGATIVO> --thread-id <TOPIC_ID>
espelho-zap --config <CONFIG> route set <CONVERSATION> <CHAT_ID_NEGATIVO> <TOPIC_ID>

# ou criar um tópico no fórum humano já existente e gravar a rota exata
espelho-zap --config <CONFIG> route provision-topic <CONVERSATION> \
  <CHAT_ID_NEGATIVO> <NOME_DO_TOPICO> --confirm-create

# liberar explicitamente eventos retidos depois de a rota estar pronta
espelho-zap --config <CONFIG> route reconcile <CONVERSATION_OPACA>

# processar no máximo uma entrega segura
espelho-zap --config <CONFIG> worker-once --profile default
```

IDs e conteúdo não aparecem no diagnóstico por padrão. Use as opções de
identificadores exatos apenas durante uma manutenção autorizada.

## Adaptadores

- `integrations/hermes/`: plugin `pre_gateway_dispatch`, antes da LLM. Observa
  WhatsApp e o fórum Telegram. Inbound WhatsApp é capturado e nunca chega ao
  agente. No fórum, uma mensagem humana allowlisted é reservada como outbound
  para a rota exata do tópico e também retorna `action=skip`.
- `integrations/openclaw/`: captura por `inbound_claim`/`message_received`,
  silêncio em `before_agent_reply` e cancelamento defensivo em
  `message_sending`/`reply_payload_sending`. Requer a emissão explícita do hook
  WhatsApp e o consentimento de conversation hooks na configuração. Com opt-in,
  o mesmo `message_received` consome mensagem humana no fórum exato, reserva em
  ledger privado e usa o adapter nativo do canal WhatsApp já pareado; o fórum
  permanece fora da LLM e nenhum segundo cliente é criado.
- `src/espelho_zap/adapters/openclaw_jsonl.py`: compatibilidade read-only com
  sessões JSONL antigas, com cursor por geração e linha parcial.

Os adapters de captura em `src/espelho_zap/adapters/` devem produzir o mesmo
`InboundEvent`; eles não importam clientes de envio WhatsApp nem escolhem
destinos Telegram. As integrações de runtime implementam, separadamente, a
lane de outbound humano descrita acima e nunca aceitam destino informado no
texto da mensagem.

Destinos prepared-only gerenciados pelo instalador, ambos fora de discovery:

- Hermes: `${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/runtime-staging/hermes/espelho-zap-portable`;
- OpenClaw: `${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/runtime-staging/openclaw/espelho-zap-portable`,
  ainda exigindo `--runtime-home` explícito para selecionar o futuro alvo.

“Instalado/preparado” não significa “habilitado/carregado”. A ativação OpenClaw
é transacional e só conclui depois do canário de runtime; a ativação Hermes
automatizada permanece bloqueada nesta versão.

## Migração da legacy runtime

O importador reutiliza o que já foi construído na legacy runtime, em vez de redesenhar:

- `groupChatId`;
- `contactTopics` e seus `topicId`;
- `lastRoutedInboundMessageId`;
- `recentRoutedInboundMessageIds` (limite histórico de 500 por tópico);
- dedupe/watermark para não reenviar o passado.

O import é aditivo e repetível. `topicName` é apenas um rótulo; nunca decide a
rota. Tokens, sessões e o perfil completo da legacy runtime não são importados.

## Produto versus projeções

O ledger é a fonte operacional. Daily Notes, relatórios, claims e exportação
para busca/GBrain são consumidores separados, com cursor próprio e privacy
scope. GBrain é opcional e reconstruível; não é requisito para capturar ou
espelhar uma mensagem. Nesta versão, essas projeções são APIs Python opcionais
exportadas por `espelho_zap` (`MirrorConsumers` e tipos relacionados); não há
ainda CLI, daemon, timer ou ativação automática para consumers.

## Outbound humano

O espelho completo é bidirecional para o humano: escrever normalmente no
tópico mapeado envia texto ou mídia à conversa WhatsApp — contato ou grupo — daquele tópico. Não se
usa `/wa` e a mensagem não passa pela LLM. O bridge continua bloqueando todo
outbound genérico ou automático. No OpenClaw, o envio humano usa o adapter
nativo do canal já pareado; no Hermes, usa o bridge local autenticado. Contrato,
tipos, dedupe, falha ambígua e diferenças para a legacy runtime estão em
`docs/HUMAN_OUTBOUND.md`. O ARM do Hermes é ligado ao commit, aos bytes do
plugin e ao fingerprint do executável/venv/gateway/loader; uma atualização
invalida o ARM anterior e mantém outbound fechado até novo marker e rearme.

## Conteúdo versionado

| Caminho | Função |
| --- | --- |
| `src/espelho_zap/` | núcleo, CLI, adapters e consumidores |
| `integrations/` | plugins finos Hermes/OpenClaw |
| `config/` | configuração sem segredo |
| `installer/` e `packaging/` | instalação e systemd |
| `skills/espelho-zap-portable/` | operação assistida por agente |
| `.specs/` e `docs/` | requisitos, arquitetura, aceite e rollback |
| `migration/` | bundle/manifesto de migração legado |
| `capture_v2/` e `mirror/` | baseline preservada da legacy runtime |
| `tests*` | testes sem rede e fixtures sintéticas |

Nunca versionar banco real, conversas, contatos, telefones, mídia, tokens,
cookies, sessões autenticadas ou bundles reais neste diretório.

## Testes

```bash
python -m pytest -q tests_core tests_consumers tests_packaging tests \
  mirror/tests capture_v2/tests
python -m compileall -q src integrations
python -m pip install --upgrade "setuptools>=68" wheel build
python -m build --sdist --wheel
```

A aprovação automática não substitui o canário humano. Produção só é aceita
quando texto, foto e áudio novos chegam uma única vez aos tópicos corretos,
nenhum deles cai na DM e um ciclo de replay confirma zero duplicata.

## Distribuição

A versão `0.3.2` é licenciada sob Apache-2.0, conforme `LICENSE` e o metadata do
pacote. Redistribuição deve preservar os avisos exigidos pela licença.
Publicação aberta em PyPI, ClawHub ou outro catálogo ainda exige o gate de
higiene do artefato: nenhum dado, estado, credencial ou path privado do host.

O wheel instala o pacote Python e o entry point `espelho-zap`; ele não contém o
instalador, a skill, os plugins nem as unidades systemd. O pacote completo para
compartilhamento é o par wheel + sdist da mesma versão. Extraia o sdist para
obter esses artefatos e execute `installer/install.sh --source` apontando para o
wheel correspondente, conforme `docs/INSTALLATION.md`.

## Roteamento documental

- requisitos: `docs/PRD.md` e `.specs/features/portable-product/spec.md`;
- arquitetura: `docs/ARCHITECTURE.md`;
- instalação/rollback: `docs/INSTALLATION.md`;
- adapters: `docs/ADAPTER_CONTRACT.md`;
- aceite real: `docs/ACCEPTANCE_TESTS.md`;
- migração ampla: `docs/MIGRATION_RUNBOOK.md`;
- memória: deployment-specific memory (not included);
- defeitos históricos: the product release history;
- execução/handoff Hermes: the product handoff record.

## Confirmação de escopo

Este repositório entrega o produto portátil e seus contratos. A ativação de
canal real, cutover e exclusão da legacy runtime exigem o runbook, backup/rollback e
canário da versão instalada. O outbound humano faz parte do produto, mas sua
ativação também exige rota, allowlist, single-writer e canário humano.

## Contrato 0.3 — contatos, grupos, identidade e mídia

Este contrato substitui qualquer interpretação genérica de “uma conversa por
tópico”:

- conversa direta: `auto_create_direct_contact_topics=true`; o primeiro inbound
  pode criar o tópico no fórum configurado, preferindo o nome vindo do contato,
  sessão ou perfil público do WhatsApp;
- grupo WhatsApp: `auto_create_whatsapp_group_topics=false` e
  `approved_groups_only=true`; nenhum grupo é admitido ou cria tópico apenas
  por aparecer no canal;
- um grupo só entra depois de JID/conversation exato, perfil WhatsApp exato,
  tópico Telegram exato, `privacy_scope`, rota habilitada e aprovação explícita;
- a simples existência de tópico ou rota nunca autoriza agente. O default é
  `agent_mode=none`, isto é, espelho passivo sem LLM;
- `mention_only` exige o grill versionado de dez campos: nome, missão, público,
  fontes, gatilhos, permissões, proibições, aprovação/escalação, tom/SLA e
  exemplos de aceite;
- em grupo, cada mensagem/caption recebe o nome humano resolvido. A prioridade
  é alias manual, contato/sessão, label do evento e nome público/business. JID,
  telefone e identificador cru nunca são exibidos. Sem identidade segura, a
  entrega falha fechada antes do aceite;
- foto e demais mídias seguem pelo caminho determinístico, sem OCR, vision ou
  LLM automáticos. OCR/vision só podem ser executados por pedido humano explícito
  sobre uma mídia escolhida, sem alterar o item já espelhado;
- áudio/voice chega apenas como áudio reproduzível. A transcrição pode alimentar
  `context_text` internamente, mas nunca a mensagem/caption do Telegram;
- o spool gerenciado é bounded e a retenção configurada não pode ultrapassar 48
  horas. A limpeza só ocorre após ACK; Telegram permanece como cópia visual;
- recibos WhatsApp são monotônicos (`sent/device=2`, `delivered=3`, `read=4`,
  `played=5` quando o provider disponibilizar). O produto nunca inventa leitura
  nem rebaixa estado;
- testes automáticos deixam `prepared`. `installed_success` exige confirmação
  humana real, em ambos os sentidos, de texto, foto e áudio, com rota exata,
  entrega única, integridade, zero enriquecimento e zero fallback para DM.

Comandos novos do contrato:

```bash
# primeiro grave a rota exata; depois aprove o grupo passivo
espelho-zap --config <CONFIG> group approve <JID_EXATO> \
  --source-profile <PERFIL> --privacy-scope area_shared \
  --agent-mode none --confirm-approve

# habilitar agente exige o JSON completo do grill
espelho-zap --config <CONFIG> group approve <JID_EXATO> \
  --source-profile <PERFIL> --privacy-scope area_shared \
  --agent-mode mention_only --grill grill.json --confirm-approve

# corrigir/aprovar o nome de um participante sem expor JID no Telegram
espelho-zap --config <CONFIG> identity set <JID_GRUPO> <ID_PARTICIPANTE> "Nome"

# registrar cada canário real; o status só vira installed_success com a matriz 2x3
espelho-zap --config <CONFIG> acceptance status
```
