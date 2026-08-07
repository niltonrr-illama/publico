# Testes de aceite — Espelho Zap Portable

**ID:** TEST-ESPELHO-ZAP-ACCEPTANCE-001
**Tipo:** runbook de validacao
**Status:** suites locais verdes com dois skips POSIX; build Linux, runtime real e canario produtivo ainda nao aprovados
**Dono:** Maintainer
**Escopo:** WhatsApp Cockpit
**Fonte canonica:** sim, para gates de aceite
**Fonte superior:** `ARCHITECTURE.md` e `THREAT_MODEL.md`
**Pai:** `ARCH-ESPELHO-ZAP-PORTABLE-001`
**Filhos:** evidencias privadas de cada instalacao
**Relacionados:** issues #171/#176/#84; `.specs/codebase/TESTING.md`
**Substitui:** nenhum
**Substituido por:** nenhum
**Sensibilidade:** L1; evidencias reais ficam privadas
**Ultima revisao:** 2026-08-06

## 1. Regra de aceite

Ha quatro gates independentes:

1. **codigo:** unit/integration/build;
2. **instalacao:** wheel/config/DB/rollback em ambiente limpo;
3. **staging:** adapter, importacao, rotas e fake transport sem canal;
4. **producao:** single-writer e canario humano real.

Passar um gate nao implica passar os demais. Um ACK, commit, bot respondendo, HTTP 200 ou ausencia de log de erro nao e aceite completo.

### 1.1 Snapshot de evidencia atual

Execucao local de 2026-08-04, sem canal/runtime real:

| Suite | Resultado | O que comprova |
| --- | --- | --- |
| `capture_v2/tests` | 18 PASS | baseline Captura V2 |
| `mirror/tests` | 6 PASS | politica de imagem/legenda OpenClaw |
| `tests` | 5 PASS | bundle/readiness brownfield |
| `tests_core` | 79 PASS, 2 SKIP POSIX/Linux | modelos, ledger, rotas, adapters, delivery ledger, worker e Telegram fake |
| `tests_consumers` | 11 PASS | Daily Notes, claims, busca e relatorios |
| `tests_packaging` | 18 PASS, 1 SKIP POSIX/Linux | config, CLI, backup, installer, units e skill |

Total: **137 PASS e 3 SKIP dependentes de POSIX/Linux**. `tests_packaging` tambem passou com `ResourceWarning` promovido a erro depois de corrigido o fechamento do handle criado pelo proprio teste.

Status dos quatro gates:

| Gate | Status atual | Motivo |
| --- | --- | --- |
| codigo | PARTIAL | suites Python locais, build wheel/sdist e smoke de wheel no Windows verdes; checks POSIX/Linux aguardam CI |
| instalacao | NOT_RUN | nenhum wheel/installer foi promovido e testado em Linux limpo nesta evidencia |
| staging | NOT_RUN | plugins/adapters reais, import dry-run e topicos reais nao foram exercitados |
| producao | NOT_RUN | single-writer, canario humano, replay/restart e rollback continuam abertos |

As tabelas seguintes sao criterios de aceite. Uma linha sem evidencia registrada nao herda `PASS` do snapshot acima.

## 2. Preflight

Antes de qualquer teste real:

- [ ] versao/commit/hash registrados;
- [ ] `git status`/artefato sem mudancas nao rastreadas no escopo;
- [ ] backup e restore drill validos;
- [ ] SQLite `quick_check=ok`;
- [ ] config valida e secret scanner sem achado;
- [ ] rota de canario com `chat_id + message_thread_id` confirmada;
- [ ] DM nao aparece como target/default;
- [ ] origem/destino comprovam um unico writer;
- [ ] outbound WhatsApp automático/LLM desabilitado;
- [ ] outbound humano permanece desabilitado até rota, allowlist e bridge passarem;
- [ ] consumers/cron/GBrain desligados para o primeiro canario;
- [ ] WIP=1;
- [ ] humano sabe exatamente o que enviar/observar;
- [ ] rollback pode ser executado sem apagar estado.

## 3. Suites automaticas obrigatorias

Executar e preservar os 29 testes existentes:

```bash
python -m unittest discover -s capture_v2/tests -p "test_*.py" -v
python -m unittest discover -s mirror/tests -p "test_*.py" -v
python -m unittest discover -s tests -p "test_*.py" -v
```

Resultado de referencia em 2026-08-04: 18 + 6 + 5 = 29 PASS. O total futuro deve ser maior ou igual; reduzir exige explicacao e aprovacao, nunca exclusao silenciosa.

Executar tambem as suites do produto graduado:

```bash
python -m unittest discover -s tests_core -p "test_*.py" -v
python -m unittest discover -s tests_consumers -p "test_*.py" -v
python -W error::ResourceWarning -m unittest discover -s tests_packaging -p "test_*.py" -v
```

Em Linux, nenhum skip POSIX e aceito no fechamento. Registrar comando, Python, OS, exit code e total por suite.

## 4. Matriz automatica do core

| ID | Cenario | Acao | Esperado |
| --- | --- | --- | --- |
| AT-CAP-01 | evento novo | inserir envelope valido | 1 evento, cursor avanca na mesma transacao |
| AT-IDEM-01 | replay identico | ingerir duas vezes | 1 evento; duplicate counter +1 |
| AT-IDEM-02 | mesmo ID/body diferente | ingerir conflito | fail closed; evento original intacto |
| AT-IDEM-03 | restart | fechar/reabrir e poll | zero reinsercao/reenvio indevido |
| AT-ROUTE-01 | rota valida | resolver route key | par chat/thread exato |
| AT-ROUTE-02 | rota ausente | projetar evento | `blocked_no_route`, zero transport call |
| AT-ROUTE-03 | rota desativada/incompleta | projetar evento | bloqueio, zero fallback |
| AT-ROUTE-04 | nomes/telefones/titulos iguais | resolver | nao participam da identidade |
| AT-PLANE-01 | DM disponivel como contexto | evento sem rota | zero chamada DM/last-chat/default |
| AT-DELIVERY-01 | sucesso confirmado | fake transport | `sent` + remote ID + media ACK |
| AT-DELIVERY-02 | erro antes do send | fake falha segura | retry bounded/backoff |
| AT-DELIVERY-03 | timeout ambiguo | fake aceita e expira | `uncertain`; zero retry automatico |
| AT-OUTBOUND-01 | outbound automatico/LLM/bot | hooks defensivos | rejeitado; zero cliente send |
| AT-OUTBOUND-02 | humano allowlisted no topico mapeado | texto e midias suportadas | destino exato; uma entrega por message_id |
| AT-OUTBOUND-03 | parcial/timeout/restart/replay | fake bridge + reopen | `uncertain` sem retry; `prepared` seguro retoma; zero duplicata |
| AT-OUTBOUND-04 | provider receipt | `message-receipt.update`/`messages.update` | ledger avanca monotonicamente; recibo invalido fica sem ACK |
| AT-LEASE-01 | duas instancias | adquirir mesmo perfil | segunda falha antes de poll/send |
| AT-PROMPT-01 | mensagem com comandos | ingerir texto hostil | corpo armazenado; rota/config/lease inalterados |

## 5. Banco e migracao

| ID | Cenario | Esperado |
| --- | --- | --- |
| AT-DB-01 | DB novo | schema completo e quick-check ok |
| AT-DB-02 | DB Captura V2 existente | `events`/`cursors` preservados; apenas `mirror_*` aditivo; sem takeover de `PRAGMA user_version` |
| AT-DB-03 | schema futuro | bloqueia antes de escrever |
| AT-MIG-01 | import legacy runtime dry-run | contagens/conflitos/watermark; zero escrita/conteudo |
| AT-MIG-02 | import real + segunda execucao | rotas/dedupe iguais; zero duplicata/replay anterior ao watermark |
| AT-MIG-03 | fixture com `topicName` alterado | mesma rota por chave/IDs; nome ignorado como identidade |
| AT-UPGRADE-01 | upgrade/rollback | backup, migrate, quick-check, restore e hashes comprovados |
| AT-UPGRADE-02 | executavel/venv/gateway/loader Hermes muda | fingerprint muda; ARM antigo bloqueia antes de preparar/enviar |
| AT-UPGRADE-03 | YAML Hermes malformado | config validate falha antes do restart e a transacao restaura o estado anterior |

O importador legacy runtime deve reconhecer o shape legado real: `groupChatId` na raiz e `contactTopics`, cujos itens possuem `topicId`, `topicName`, `lastRoutedInboundMessageId` e `recentRoutedInboundMessageIds`. Ele reutiliza `groupChatId`/`topicId`, dedupe IDs e watermark; `topicName` e apenas label. A chave da conversa e persistida de forma opaca/hashed.

Estado atual: importacao/idempotencia real em fixture PASS; `AT-MIG-01` continua `NOT_RUN` porque a CLI atual ainda nao oferece dry-run de `route import-legacy runtime`. Nao substituir esse gate por importacao direta no banco ativo. Ate existir dry-run, usar apenas copia descartavel do DB e comparar hashes/contagens antes de autorizar mutacao do staging.

## 6. Midia e filesystem

| ID | Cenario | Esperado |
| --- | --- | --- |
| AT-MEDIA-01 | foto com legenda | bytes/hash intactos; legenda exata |
| AT-MEDIA-02 | wrapper `Description:`/OCR | removido pela defesa; guard primario impede analise no inbox |
| AT-MEDIA-03 | voice e audio | voice via `sendVoice`; audio via `sendAudio`; ambos reproduziveis |
| AT-MEDIA-04 | processamento derivado falha | original continua elegivel para entrega |
| AT-MEDIA-05 | legenda acima do limite | erro/gate ou estrategia versionada; nunca truncamento silencioso |
| AT-MEDIA-06 | ACK ausente/`uncertain` | arquivo nao e purgado |
| AT-MEDIA-07 | ACK confirmado + retencao vencida | temporario removido; evidencia/hash ficam |
| AT-FS-01 | symlink/path traversal | recusado antes da leitura/copia |
| AT-FS-02 | arquivo muda durante operacao | `media_changed`, nenhum envio |
| AT-DISK-01 | warning/hard quota | alerta agregado; hard stop seguro; nenhuma limpeza aleatoria |
| AT-PERM-01 | POSIX | dirs 0700; DB/spool files 0600 |

Estado atual: validacao/hash, tipos de Telegram, limites, streaming, purge opt-in/contido e disk floor tem testes locais. Janela temporal de retencao, quota/hard-stop do spool e modos POSIX permanecem `NOT_RUN`; portanto o gate de midia ainda nao esta fechado.

## 7. Adapters

Rodar a mesma contract suite no OpenClaw e Hermes:

- descriptor/capabilities/API version;
- lote bounded;
- replay/IDs/cursor;
- parcial/malformado;
- rotacao de fonte;
- tipos de midia;
- materializacao segura;
- scope;
- zero outbound automático;
- outbound humano não cruza DM, autor ou tópico;
- schema incompativel.

### OpenClaw

- [ ] seleciona apenas sessoes WhatsApp elegiveis;
- [ ] ignora trajectory/checkpoint/reset;
- [ ] image scope deny apenas no prefixo inbox;
- [ ] vision continua disponivel fora desse scope;
- [ ] bootstrap EOF/watermark funciona.

### Hermes

- [ ] usa pareamento existente; zero novo QR/sessao;
- [ ] fonte real/versionada foi identificada, nao presumida;
- [ ] shim nao altera gateway/canal durante testes offline;
- [ ] envelope e byte-equivalente semanticamente ao fixture OpenClaw;
- [ ] skill descoberta nao ativa servico automaticamente.

Paths reais do candidato:

- contrato: `src/espelho_zap/adapters/base.py`;
- OpenClaw JSONL: `src/espelho_zap/adapters/openclaw_jsonl.py`;
- plugin OpenClaw: `integrations/openclaw/`;
- plugin Hermes: `integrations/hermes/`.

Nao existem atualmente `src/espelho_zap/adapters/openclaw.py` ou `adapters/hermes.py`; documentos e comandos de teste devem usar os paths acima. Contract/static tests locais PASS nao marcam os checkboxes de runtime real.

## 8. Consumidores

| ID | Cenario | Esperado |
| --- | --- | --- |
| AT-CONSUMER-01 | Daily Notes falha | cursor do espelho/captura continua |
| AT-CONSUMER-02 | rerun Daily Notes | saida idempotente por data/scope |
| AT-SCOPE-01 | claim tenta scope menos restritivo | rejeitado; evidencia/evento intactos |
| AT-CONSUMER-03 | apagar/recriar indice | ledger/claims iguais; indice reconstruido |
| AT-CONSUMER-04 | provider/LLM offline | eventos permanecem nao curados; espelho continua |

Implementacao fisica atual: `src/espelho_zap/consumers.py`. `tests_consumers/test_consumers.py` cobre Daily Notes, claims/evidencias, busca provider-agnostic e relatorios; nenhum provider/GBrain real foi ativado. O indice continua opcional e reconstruivel.

## 9. Observabilidade e distribuicao

| ID | Cenario | Esperado |
| --- | --- | --- |
| AT-OBS-01 | doctor/log/health | apenas contagens, estados, versions, hashes opacos, bytes e timestamps |
| AT-SECRET-01 | scanner | zero token/chave/cookie/sessao/URL autenticada no pacote/log |
| AT-DIST-01 | wheel/skill/docs | zero dado/ID/path/nome ExampleCo; fixtures sinteticas |
| AT-OPS-01 | dry-run mutante | zero escrita/restart/canal |

## 10. Instalacao limpa

O entrypoint de lifecycle atual e `installer/install.sh`; a CLI nao possui `init --dry-run`, `config validate`, `db migrate`, `restore` ou `upgrade`. Nao documentar esses nomes como comandos existentes. Em host Linux/venv isolado:

1. provisionar `setuptools>=68`, `wheel` e `build`, construir exatamente um
   wheel e um sdist da mesma versão e registrar o SHA-256 dos mesmos bytes no
   manifesto;
2. `python -m venv` limpo, instalar o wheel e rodar `espelho-zap --help`/`--version`;
3. `./installer/install.sh preflight --source /caminho/pacote.whl`;
4. `./installer/install.sh install --source /caminho/pacote.whl --dry-run`;
5. executar install real em XDG/HOME temporarios, comprovando timer desabilitado;
6. `espelho-zap --config PATH doctor --allow-missing-token` e `health`;
7. em supergrupo fórum criado uma vez por uma pessoa, provar que
   `route provision-topic ... --confirm-create` cria o tópico exato e grava a
   rota; sem confirmação ou em DM, provar zero criação/fallback;
8. criar fixtures sinteticas com `route set`, `ingest`, `blocked-list` e `reconcile`;
9. provar `backup DESTINO_NOVO`, `quick_check=ok`, hash e nao-sobrescrita;
10. manter uma transação mutante com o lock global e provar que install, upgrade
   e uninstall concorrentes recusam antes de escrever;
11. executar upgrade real e rollback induzido em sandbox, comprovando que
    timer/worker e gateway ficam quiescentes antes dos snapshots e que bytes de
    config, ledger, health, registros, units e índice/plugin voltam exatamente;
12. executar `uninstall --dry-run`, depois uninstall real, comprovando
    preservação de config/token/ledger/health/state/backups e restauração de
    units preexistentes, inclusive estado active/enabled;
13. validar `packaging/systemd/espelho-zap@.service` e `.timer`, sem habilitá-los;
14. validar a skill e a cópia prepared-only em
    `runtime-staging/<alvo>/espelho-zap-portable`, provando que home, config e
    raiz de discovery Hermes/OpenClaw não foram criados/tocados;
15. repetir `--media-root` com duas raízes temporárias e provar a mesma lista no
    TOML/template; depois omitir a opção em upgrade e provar preservação;
16. usar `--clear-media-roots` e provar lista vazia; provar também que
    `worker.profile_id`, env do source profile e demais campos `[worker]`, como
    o limite de spool, permanecem corretos;
17. provar criação `0600` de `capture-health.json` com schema agregado, sem
    conteúdo, e atualização sanitizada por sucesso/falha dos adapters;
18. pedir `--runtime hermes --enable-runtime` e provar recusa no gate que não
    consegue demonstrar hook carregado no gateway; CLI e staging permanecem;
19. no OpenClaw fake, executar `--enable-runtime` e provar install provenance,
    preservação dos outros itens de allow/deny,
    `channels.whatsapp.pluginHooks.messageReceived=true`,
    `hooks.allowConversationAccess=true`, restart e RPC profundo;
20. exigir que `plugins inspect --runtime` prove `message_received`,
    `before_agent_reply`, `message_sending` e `reply_payload_sending`; retirar
    qualquer um deles deve bloquear a ativação;
21. induzir falha depois das mutações OpenClaw e comparar config/units/ledger
    restaurados, índice SQLite anterior, plugin e estados running/stopped;
22. com integração ativa, provar que uninstall sem seleção recusa antes de
    remover a CLI; depois usar runtime/home exatos e provar desativação oficial,
    restauração field-scoped do baseline, ausência do plugin e só então remoção
    da CLI/activation record;
23. provar que omitir a seleção no uninstall preserva staging prepared e que a
    seleção explícita remove somente a cópia com marcador gerenciado.

O fake transport e exercitado pelos testes Python, nao por `worker-once`: o comando real resolve token e pode fazer rede. Toda etapa registra exit code, versao/hash e permissao; nenhum dado real e necessario.

### 10.1 Superficie CLI atual

Somente comandos implementados podem aparecer em runbooks executáveis da versão 0.3.1:

```text
init
doctor
health
backup
route set
route list
route blocked-list
route reconcile
route verify-destination
route provision-topic
route import-legacy runtime
ingest
worker-once
```

Upgrade/rollback/uninstall sao operacoes do instalador. Restore destrutivo e migracao manual de schema nao possuem CLI publica nesta versao.

## 11. Canario humano real

### Preparacao

- escolher uma conversa explicitamente mapeada;
- confirmar o topico no cliente Telegram;
- zerar somente contadores do canario, nunca logs/historico;
- iniciar monitor agregado de eventos/outbox/delivery/outbound;
- manter WIP=1.

### Execucao

Operator/remetente autorizado envia separadamente:

1. um texto curto;
2. uma foto nova com legenda curta;
3. uma voice note curta;
4. opcionalmente, um audio generico para diferenciar `sendAudio`.

O monitor nao envia o canario, nao responde WhatsApp e nao usa simulacao como substituto.

### Confirmacao humana

- [ ] cada item apareceu uma unica vez;
- [ ] apareceu no topico correto do grupo, nao na DM;
- [ ] texto e legenda preservados;
- [ ] foto integra e sem `Description:`, OCR ou analise;
- [ ] voice reproduz no formato esperado;
- [ ] audio generico, se testado, aparece como audio;
- [ ] nenhum outbound WhatsApp automático;
- [ ] nenhum outro topico recebeu copia.

### Pos-canario

- reiniciar apenas se o plano de aceite exigir;
- executar replay/novo poll sem novo inbound;
- confirmar zero segunda entrega;
- confirmar outbox `sent`, remote IDs presentes e `uncertain=0` para o canario;
- confirmar purge conforme janela, nao imediatamente antes do ACK;
- rodar dois ciclos saudaveis antes de ampliar consumers/cadencia.

### Canario de outbound humano

Depois do inbound aprovado, Operator envia no topico de canario, sem `/wa`:

1. um texto comum;
2. uma imagem com legenda;
3. uma voice note ou audio;
4. opcionalmente video e documento.

Aceite: cada item chega uma vez ao contato WhatsApp da rota, conteudo/legenda
permanece intacto, outro usuario e outro topico produzem zero envio, replay do
mesmo `message_id` produz zero envio e timeout simulado fica `uncertain` sem
retry. Nenhum desses eventos entra na LLM.

## 12. Stop/rollback conditions

Parar o destino e manter estado se ocorrer:

- destino errado ou DM;
- duplicata;
- `Description:`/OCR/analise;
- outbound WhatsApp automatico, para destino errado ou duplicado;
- dois writers;
- quick-check diferente de `ok`;
- scope leak;
- purge sem ACK;
- crescimento de spool sem limite;
- crash/restart loop;
- schema ou adapter incompativel.

Rollback segue `INSTALLATION.md`: destino para primeiro; origem so retoma depois de zero writer no destino e reconciliacao do watermark.

## 13. Evidencia minima

Registrar sem conteudo:

```text
version/commit/hash:
runtime/adapter versions:
database schema + quick_check:
route count/status:
events delta:
outbox/delivery states:
single-writer proof:
media hashes/bytes only:
human visual confirmation:
outbound count:
restart/replay result:
rollback proof:
blockers/residual risk:
```

Status permitido: `PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`. Nunca usar “OK geral” se algum caso nao foi executado.

## 14. Definition of accepted

Aceite produtivo requer:

- automatic suites/build/install/transactional rollback PASS;
- adapter alvo PASS;
- import/route/watermark PASS;
- single-writer PASS;
- canario humano PASS;
- replay/restart PASS;
- rollback ready;
- zero P1 aberto.

## 15. Gates de publicacao e handoff

Antes de dizer ao Hermes/OpenClaw para instalar:

- [ ] commit/artefato exato publicado e relido;
- [ ] wheel/sdist e manifest apontam para os mesmos bytes testados;
- [ ] versão do metadata e dos nomes do wheel/sdist é idêntica;
- [ ] docs nao contem path/comando conceitual inexistente;
- [ ] `LICENSE` e metadata declaram Apache-2.0 e os avisos acompanham o artefato;
- [ ] instalacao e importacao sao executadas do artefato, sem reconstruir do zero;
- [ ] o runtime responde na mesma issue com versao/hash/gates e sem ecoar segredo;
- [ ] canario humano permanece explicitamente pendente ate execucao real.
- [ ] nenhum runtime foi declarado ativo apenas por discovery ou cópia no disco;

## Roteamento / Proximo passo

Se voce chegou aqui procurando:
- instalar/rollback -> leia `INSTALLATION.md`;
- ameacas -> leia `THREAT_MODEL.md`;
- adapter -> leia `ADAPTER_CONTRACT.md`;
- requisitos -> leia `../.specs/features/portable-product/spec.md`.

## Confirmacao de escopo

Este documento trata dos testes automaticos, staging, canario humano, evidencia e rollback.
Este documento nao substitui a autorizacao operacional para ativar um canal ou
dois writers. O outbound humano descrito e parte do produto; so recebe `PASS`
depois do canario real e do rollback comprovado.
Fonte canonica superior: `ARCHITECTURE.md` e `THREAT_MODEL.md`.

## 16. Matriz obrigatória 0.3

| Gate | Prova humana necessária |
| --- | --- |
| inbound texto/foto/áudio | uma entrega, tópico exato, sem DM e sem enriquecimento |
| outbound texto/foto/áudio | uma entrega à conversa exata, conteúdo íntegro |
| grupo | dois participantes com nomes distintos, nenhum JID/telefone cru |
| grupo `mention_only` | responde com menção e permanece passivo sem menção |
| recibos | sent/delivered e read/played apenas quando realmente reportados |
| retenção | sem purge antes do ACK; sem arquivo gerenciado com mais de 48 horas |

Cada canário aceito deve ser registrado por `acceptance record` com todas as
assertivas e uma referência opaca de evidência. A ausência de qualquer uma das
seis combinações principais mantém `acceptance status=prepared`.
