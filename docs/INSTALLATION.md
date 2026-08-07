# Instalação, upgrade e rollback — Espelho Zap Portable

**ID:** RUNBOOK-ESPELHO-ZAP-INSTALL-001
**Status:** candidato `0.3.2`; aceite humano bidirecional ainda pendente
**Escopo:** instalação Linux por usuário, configuração, rotas, upgrade e rollback
**Sensibilidade:** L1; dados e credenciais reais ficam fora do Git
**Última revisão:** 2026-08-06

Este runbook usa somente comandos existentes nesta versão. Instalar não ativa
o canal: o timer systemd é entregue desabilitado.

Estado em 2026-08-04: o smoke Linux está versionado, mas não foi executado neste
host Windows sem daemon Docker/distribuição WSL. Nenhuma integração de runtime
foi ativada nem recebeu canário real neste host.

## 1. Gates antes da instalação

Confirme:

- Linux, Python 3.11 ou superior, `venv`, SQLite e espaço livre suficiente;
- checkout privado ou wheel/sdist recebidos por canal autorizado e com hash
  verificado;
- WhatsApp já pareado em exatamente um runtime, sem criar um segundo cliente;
- supergrupo Telegram com Topics criado uma única vez por uma pessoa, e bot
  autorizado como admin com `can_manage_topics`;
- configuração, token, ledger, mídia e backups fora do Git;
- janela de canário e autoridade explícita para ativar um único writer.

O instalador aplica um piso de 256 MiB por padrão. Ajuste apenas com uma decisão
de capacidade explícita:

```bash
export ESPELHO_ZAP_MIN_FREE_BYTES=268435456
```

## 2. Artefato completo para compartilhamento

O wheel contém o pacote Python e o entry point `espelho-zap`. Ele não contém o
instalador, a skill, os plugins nem as unidades systemd. O pacote completo é o
par sdist + wheel da mesma versão. Código e artefatos portáteis são Apache-2.0;
preserve `LICENSE` e os avisos exigidos ao redistribuir:

```bash
python -m pip install --upgrade "setuptools>=68" wheel build
python -m build --sdist --wheel
sha256sum dist/espelho_zap_portable-0.3.2.tar.gz \
  dist/espelho_zap_portable-0.3.2-py3-none-any.whl \
  | tee dist/SHA256SUMS
```

O gate de publicação exige exatamente um wheel e um sdist da mesma versão. O
manifesto `SHA256SUMS` deve ser produzido depois do build e acompanhar esses
mesmos bytes; não reutilize um manifesto de uma compilação anterior.

No host de destino, mantenha os dois arquivos juntos, valide os hashes e extraia
o sdist. Execute o instalador extraído apontando para o wheel correspondente:

```bash
tar -xzf espelho_zap_portable-0.3.2.tar.gz
cd espelho_zap_portable-0.3.2
bash installer/install.sh preflight --source ../espelho_zap_portable-0.3.2-py3-none-any.whl
bash installer/install.sh install --source ../espelho_zap_portable-0.3.2-py3-none-any.whl --dry-run
bash installer/install.sh install --source ../espelho_zap_portable-0.3.2-py3-none-any.whl
```

Em um checkout privado, `--source .` também é aceito. A instalação local exige
`setuptools>=68` já disponível no venv candidato; quando isso não estiver
garantido, use o wheel pré-construído.

## 3. Layout por usuário

Com os XDG defaults, o instalador gerencia:

```text
~/.config/espelho-zap/config.toml                 configuração 0600
~/.config/espelho-zap/telegram.token              token 0600
~/.local/share/espelho-zap/venv/                  aplicação
~/.local/share/espelho-zap/mirror.sqlite3         ledger privado
~/.local/share/espelho-zap/backups/               backups privados
~/.local/share/espelho-zap/install.state          ownership do instalador
~/.local/share/espelho-zap/runtime-activation.state  integração ativa, se houver
~/.local/share/espelho-zap/runtime-staging/<alvo>/espelho-zap-portable/
                                                    plugin prepared, fora de discovery
~/.local/state/espelho-zap/capture-health.json    saúde agregada dos hooks, 0600
~/.config/systemd/user/espelho-zap@.service       serviço oneshot por perfil
~/.config/systemd/user/espelho-zap@.timer         timer desabilitado
~/.agents/skills/espelho-zap-portable/            cópia gerenciada da skill
~/.hermes/plugins/espelho-zap-portable/           discovery Hermes, só se ativado
<OPENCLAW_HOME>/extensions/espelho-zap-portable/  discovery OpenClaw, só se ativado
```

Diretórios privados são criados com modo `0700` e arquivos sensíveis com
`0600`. O instalador rejeita symlinks nos destinos gerenciados e só remove venvs
e skills dentro de suas raízes esperadas.

Uma execução real de install/upgrade/uninstall adquire, sem espera, o lock
global `${XDG_RUNTIME_DIR:-/tmp}/espelho-zap-portable-installer-${UID}/transaction.lock`.
Se outra transação estiver ativa, a segunda falha antes de mutar estado.
`preflight` e `--dry-run` continuam não mutantes e não criam o lock.

Defina atalhos para os comandos abaixo:

```bash
ESPELHO_ZAP_BIN="${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/venv/bin/espelho-zap"
ESPELHO_ZAP_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/espelho-zap/config.toml"
```

## 4. Configuração e segredo

O instalador executa `init` e cria uma configuração secret-free. Para uma
inicialização manual equivalente:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" init \
  --data-dir "${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap"
```

O shape real está em `config/config.example.toml`. Os campos operacionais são
`[paths]`, `[telegram]`, `[worker]` e `[legacy]`; não existem nesta versão
subcomandos `config`, `db`, `run`, `upgrade` ou `restore`.

Nunca grave o token literal no TOML. Use `telegram.token_env` ou o arquivo
referenciado por `telegram.token_file`. Para o arquivo padrão:

```bash
install -m 0600 /dev/null "${XDG_CONFIG_HOME:-$HOME/.config}/espelho-zap/telegram.token"
```

Edite-o interativamente, sem ecoar o token em shell history ou logs. Depois:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" doctor
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" health
```

Durante a instalação, antes de provisionar o token, somente este diagnóstico
relaxado é apropriado:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" doctor --allow-missing-token
```

## 5. Backup verificado

`backup` usa a API online do SQLite, executa `quick_check` na origem e na cópia,
faz `fsync`, calcula SHA-256 e publica atomicamente um arquivo novo. Um caminho
existente nunca é substituído e o JSON não ecoa paths.

```bash
BACKUP="${XDG_DATA_HOME:-$HOME/.local/share}/espelho-zap/backups/manual-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" backup "$BACKUP"
```

Retenha o SHA-256 reportado junto do inventário externo. Esta versão não possui
comando de restore. Restauração exige writer parado, cópia preservada do estado
atual, validação independente do hash/SQLite e autorização específica.

## 6. Verificar e provisionar tópicos/rotas

Uma pessoa cria uma única vez o supergrupo Telegram, habilita Topics e concede
ao bot `can_manage_topics`. O produto não cria grupos nem usa DM. Dentro desse
fórum já existente, o pacote pode criar cada tópico necessário e persistir a
rota exata sob confirmação explícita.

Antes de cadastrar ou provisionar uma rota, verifique o destino sem enviar
mensagem:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" \
  route verify-destination -1001234567890 --thread-id 42
```

O comando faz uma única chamada Telegram `getChat`. O sucesso exige que o ID
retornado seja o solicitado, `type=supergroup` e `is_forum=true`. Ele não chama
nenhum método de envio. `getChat` não prova que o tópico `42` existe; o parâmetro
apenas valida que o ID é inteiro positivo.

Para criar um tópico no fórum verificado e gravar seu `chat_id` + `thread_id`
exatos em uma única operação:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" \
  route provision-topic CONVERSATION -1001234567890 "Nome do tópico" \
  --confirm-create
```

Sem `--confirm-create`, nenhuma mutação externa ocorre. Se o tópico for criado
mas o commit local da rota falhar, o comando tenta removê-lo como compensação e
reporta falha/estado incerto de forma explícita. Ele nunca escolhe DM, último
chat, nome parecido ou primeiro tópico disponível.

Cadastre uma conversa e um tópico:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" \
  route set CONVERSATION -1001234567890 42
```

Uma conversa raw é transformada em referência opaca na fronteira da CLI. Uma
alteração de destino existente exige `--allow-update`; sem isso, a operação
falha fechada.

Inspeção padrão não revela IDs de Telegram:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" route list
```

Use `route list --show-identifiers` somente numa manutenção autorizada que
precise dos IDs exatos.

## 7. Importar rotas da legacy runtime

Antes do import, crie um backup em caminho novo. O importador reconhece
`groupChatId`, `contactTopics`, `topicId`, `lastRoutedInboundMessageId` e
`recentRoutedInboundMessageIds`. Ele não importa token, sessão ou perfil.

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" \
  route import-legacy runtime /CAMINHO/PRIVADO/legacy runtime-config.json
```

O import é aditivo e repetível. Alterar uma rota existente exige também
`--allow-update`. `topicName` nunca decide o destino.

## 8. Eventos retidos e reconciliação

Evento sem rota ou com rota desativada permanece no ledger como
`blocked_no_route`; cadastrar a rota não libera backlog automaticamente.

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" route blocked-list
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" \
  route blocked-list --state requeued --limit 500
```

A listagem retorna apenas referências opacas, estado, motivo e timestamps. Após
provisionar e verificar a rota, use a `conversation_ref` retornada para liberar
explicitamente até 500 eventos:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" \
  route reconcile conversation:HASH_HEXADECIMAL --limit 500
```

Repita apenas com evidência de que ainda há itens retidos e de que o destino
continua correto.

## 9. Ingestão e worker

Para uma fixture sintética ou para um adapter autorizado, a entrada da CLI é:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" ingest EVENT.json
```

O JSON é limitado a 4 MiB. A saída contém somente referências, contagens e
estado, nunca texto, legenda, path de mídia ou token.

Uma execução manual processa no máximo uma tentativa de entrega:

```bash
"$ESPELHO_ZAP_BIN" --config "$ESPELHO_ZAP_CONFIG" worker-once --profile default
```

Esse comando pode enviar para Telegram quando existe entrega pendente; rode-o
somente após doctor, verificação da rota, single-writer e autorização de canário.

## 10. Ativação do timer

O instalador carrega as unidades de usuário, mas não habilita o timer. Depois do
canário manual:

```bash
systemctl --user enable --now espelho-zap@default.timer
systemctl --user status espelho-zap@default.timer
```

`default` é apenas o nome estável da instância systemd. O worker lê a identidade
real de `worker.profile_id` na configuração; a unit não a substitui por `%i`.

Cada disparo chama `worker-once --profile default`. Não habilite duas instâncias
com autoridade sobre o mesmo perfil.

## 11. Adapters e skill

O instalador sempre copia a skill para `~/.agents/skills/espelho-zap-portable`.
O plugin é opt-in e há dois estados distintos:

- prepared-only (default): cópia em
  `~/.local/share/espelho-zap/runtime-staging/<alvo>/espelho-zap-portable`, fora
  das raízes de discovery, com template inerte; o home/config do runtime não é
  criado nem tocado e nenhum enable, consentimento ou restart ocorre;
- enabled: somente OpenClaw pode concluir esse fluxo automatizado nesta versão.
  Exige `--enable-runtime`, CLI oficial, snapshot transacional, restart e
  canário de carregamento antes de registrar a ativação.

Prepare um único alvo por execução:

```bash
# Hermes: runtime home default ~/.hermes
bash installer/install.sh install --source PACOTE.whl \
  --runtime hermes \
  --media-root /caminho/absoluto/para/midia-aprovada \
  --media-root /caminho/absoluto/para/cache-do-runtime/images

# OpenClaw: runtime home precisa ser explícito
bash installer/install.sh install --source PACOTE.whl \
  --runtime openclaw \
  --runtime-home "$HOME/.openclaw" \
  --media-root /caminho/absoluto/para/midia-aprovada \
  --media-root /caminho/absoluto/para/cache-do-runtime/images
```

`--media-root` é repetível. Cada raiz deve existir, ser absoluta, legível e não
ser symlink; `/` e o home inteiro são recusados por serem amplos demais. A lista
efetiva é escrita em `[worker].source_media_roots` e no template
`ESPELHO_ZAP_MEDIA_ROOTS`. O perfil efetivo é persistido em
`[worker].profile_id` e exportado como `ESPELHO_ZAP_SOURCE_PROFILE_ID`.

As raízes são deliberadamente bidirecionais: elas autorizam tanto a captura
WhatsApp → Telegram quanto o transporte humano Telegram → WhatsApp. O segundo
exemplo representa o cache de imagens do runtime, quando esse runtime salva a
mídia de outbound fora da raiz do espelho. O produto não descobre esse caminho
automaticamente e nunca aceita um cache amplo; sem a segunda raiz, texto pode
funcionar enquanto uma foto outbound é rejeitada antes do bridge com
`media_path_rejected`. OCR e visão não fazem parte do transporte e só podem
ser acionados explicitamente pelo operador.

Omissão é uma operação de preservação: em upgrade ou config existente, não
passar `--media-root` mantém as raízes já autorizadas; em instalação limpa, a
lista nasce vazia/fail-closed. Para revogar todas as raízes, use explicitamente
`--clear-media-roots` (mutuamente exclusivo com `--media-root`). O instalador
também cria `capture-health.json` em modo `0600`, com somente contadores,
timestamps e código sanitizado:

```json
{"schema_version":1,"successes":0,"failures":{},"last_success_at":"","last_failure_at":"","last_error_code":""}
```

Para ativar OpenClaw após revisão:

```bash
bash installer/install.sh upgrade --source PACOTE.whl \
  --runtime openclaw --runtime-home "$HOME/.openclaw" --enable-runtime \
  --media-root /caminho/absoluto/para/midia-aprovada \
  --media-root /caminho/absoluto/para/cache-do-runtime/images
```

OpenClaw usa `openclaw plugins install` para registrar provenance, preserva os
outros itens de `allow`/`deny`, grava somente seus campos sob
`plugins.entries.espelho-zap-portable`, habilita
`channels.whatsapp.pluginHooks.messageReceived=true` e o consentimento oficial
`hooks.allowConversationAccess=true`. O gateway só volta ao estado anterior
depois de `plugins inspect ... --runtime --json` provar `message_received`,
`before_agent_reply`, `message_sending` e `reply_payload_sending` e de o RPC
profundo responder. Isso não habilita nem pareia um segundo WhatsApp. Config com
`$include` falha fechada na ativação automatizada porque a restauração exata do
arquivo incluído não pode ser garantida; prepared-only continua disponível.

Hermes usa `pre_gateway_dispatch`: para WhatsApp, captura localmente e sempre
retorna `{"action":"skip"}`, inclusive se a captura falhar, impedindo o loop do
agente e uma resposta. Para outras plataformas retorna `None`. O CLI Hermes
atual não oferece prova de que esse hook foi carregado dentro do gateway em
execução; portanto `--runtime hermes --enable-runtime` é recusado. Sair do estado
prepared exige um procedimento humano separado e autorizado que prove, no
processo do gateway, o registro do hook e uma variação esperada em
`capture-health.json`, além de zero resposta/outbound. Sem essa evidência, o
estado correto é preparado, não ativado.

### 11.1 Observer direto Hermes preparado (opt-in root)

O plugin Hermes preparado continua compatível e não exige o observer. São duas
transações separadas: primeiro prepare CLI/plugin/skill como o usuário Hermes;
depois prepare somente o observer system-level como `root`. O segundo comando
retorna antes de qualquer venv, skill, plugin, user-unit ou `systemctl --user`,
portanto não cria instalação acidental em `/root`:

```bash
sudo -u USUARIO_HERMES bash installer/install.sh install \
  --source PACOTE.whl --runtime hermes

sudo bash installer/install.sh install --runtime hermes \
  --prepare-hermes-observer \
  --hermes-observer-profile default \
  --hermes-bridge-config /caminho/privado/default-direct-bridge.toml \
  --hermes-bridge-js /opt/hermes-bridge/bridge.js \
  --hermes-human-outbound-token-file /etc/espelho-zap/default-human-outbound.token \
  --hermes-human-outbound-media-root /var/lib/espelho-zap/default/human-outbound-media \
  --hermes-service-user espelho-zap \
  --hermes-service-group espelho-zap
```

O TOML de origem precisa declarar explicitamente `node`, `session_dir`,
`spool_file`, `cache_root`, `lock_file`, porta `3011` e as políticas do bridge.
Ele também precisa estar em modo `0600`.
O instalador substitui apenas `bridge_js`, token e media root pelos parâmetros
da linha de comando, renderiza um TOML determinístico e valida guard + launcher
sob o usuário/grupo do serviço. Paths com whitespace são recusados para não
depender de escaping implícito na unit; `%` e `@` também são recusados para não
permitir expansão de specifiers systemd ou tokens do template.

O token já deve existir, pertencer ao usuário/grupo do serviço e ter modo
`0600`; o media root deve ser `0700`. O token nunca é copiado. O config
renderizado fica `0600` e pertence ao service user/group; os dois scripts ficam
root-owned, read/execute apenas para root e grupo do serviço (`0550`), dentro de
um diretório `0750`. A base comum é root-owned `0711`, sem listagem pública.
Os pais de spool e lock, o cache root e seus subdiretórios
`images`/`documents`/`audio`, além do media root, precisam existir antes do
comando, sem symlink, pertencendo ao service user/group e em modo `0700`. Assim,
o `observer_launcher.py --check` não cria estado externo durante a transação.

O destino padrão é
`/opt/espelho-zap/hermes-observer/<profile>` e a unit de instância é
`/etc/systemd/system/espelho-zap-hermes-observer@<profile>.service`. Os roots
podem ser mudados antes da primeira instalação por
`ESPELHO_ZAP_HERMES_OBSERVER_ROOT` e `ESPELHO_ZAP_HERMES_SYSTEMD_DIR`.
O diretório pai do observer root precisa existir antecipadamente; o installer
não cria uma árvore ampla em `/opt` que pudesse sobreviver a uma falha.
`systemd-analyze verify` é executado quando disponível. A publicação usa
renames no mesmo filesystem e retém os bytes anteriores para rollback exato.
Um root anterior fica aninhado em um contêiner de backup root-only `0700`, de
modo que permissões legadas mais abertas não voltem a expor o config retido.
As preparações root são serializadas por um lock fixo sob `/tmp`, independente
de `XDG_RUNTIME_DIR`.

Falha comum, `SIGINT` ou `SIGTERM` após a criação do candidate remove staging e
restaura por rename os bytes anteriores antes de sair. Esse opt-in **não**
executa `enable`, `start` nem `restart`: a unit precisa
terminar desabilitada e inativa. O instalador falha se encontrar outra unit
observer, um `bridge.js` concorrente ou artefatos não gerenciados. Aplicar o
guard ao bridge, habilitar/iniciar a unit e executar o canário humano continuam
sendo procedimentos separados, revisados e autorizados.

No OpenClaw, `message_received` sozinho é apenas observação. O contrato passivo
usa `inbound_claim` para conversas vinculadas, `before_agent_reply` para silêncio
antes do agente e `message_sending` + `reply_payload_sending` para cancelar todo
outbound WhatsApp automático em profundidade. A lane humana opcional usa o
mesmo `message_received` no fórum Telegram exato e o adapter nativo do canal
WhatsApp já pareado em OpenClaw; não usa os hooks de resposta do agente nem
cria bridge, token ou pareamento adicional. Para os contratos
completos, leia:

- `integrations/hermes/README.md`;
- `integrations/openclaw/README.md`;
- `docs/ADAPTER_CONTRACT.md`.

Não suba um segundo cliente WhatsApp e não transforme hooks em ferramentas de
modelo ou em caminhos de envio.

## 12. Consumers opcionais

Daily Notes, claims, relatório e exportação de busca são APIs Python opcionais
em `espelho_zap.consumers`, também exportadas por `espelho_zap` através de
`MirrorConsumers` e dos tipos relacionados. Não existe nesta versão CLI,
daemon, timer ou ativação automática para consumers. Uma integração futura deve
provisioná-los separadamente, com cursor e privacy scope próprios, sem bloquear
captura ou entrega.

## 13. Upgrade

Use wheel e sdist da mesma versão. Do diretório extraído do novo sdist:

```bash
bash installer/install.sh upgrade --source ../espelho_zap_portable-0.1.1-py3-none-any.whl --dry-run
bash installer/install.sh upgrade --source ../espelho_zap_portable-0.1.1-py3-none-any.whl
```

Quando existe instalação ativa, o upgrade constrói o venv e a skill candidatos
primeiro, sem tocar a instalação corrente. Em seguida captura o estado das
units, para e verifica timer/worker; se a integração estiver ativa, exige o
mesmo `--runtime`, o mesmo `--runtime-home` e `--enable-runtime`, para e verifica
o gateway, e só então cria snapshots de config, ledger SQLite validado,
`capture-health.json`, registros de ownership, plugin/index do runtime e bytes
das units. A troca, `init`, migrações e canários acontecem com os writers
quiescentes. O gateway e as units retornam ao estado running/enabled que tinham
antes apenas depois dos gates.

Omitir `--source-profile` preserva `worker.profile_id`; omitir `--media-root`
preserva `worker.source_media_roots`. Repita essas opções apenas para alterá-las,
ou use `--clear-media-roots` para revogação explícita. Uma falha restaura os
bytes exatos de config/ledger/health/registros, venv/skill/plugin/index, units e
estado running/enabled. O snapshot e o backup verificado do ledger permanecem
retidos como evidência.

Não faça upgrade com `quick_check` diferente de `ok`, writer concorrente,
entregas `uncertain` sem triagem ou sem espaço para backup e rollback.

Hermes carrega o `.env` do perfil selecionado dentro do gateway, antes da
descoberta de plugins. Um supervisor multiperfil deve selecionar o home/perfil
correto, mas não precisa copiar o `.env` inteiro para o ambiente inicial do
processo; `/proc/<pid>/environ` sozinho não prova falha desse carregamento.
Depois de qualquer upgrade ou restart, mantenha o ARM ausente até comprovar o
marker da versão/hash e `hermes_runtime_fingerprint` esperados ligado ao PID atual,
`quick_check=ok`, bridge observe-only e single-writer. Uma linha `prepared` pode
ser preservada/rearmada depois desses gates. Uma linha `sending` apenas adia o
poll quando o ARM atual já coincide; com ARM incompatível ela falha fechada.
Mudança futura de API
que viole esse gate é falha fechada e exige compatibilidade/canário antes do
rearme; nunca trate gateway vivo como prova suficiente.

## 14. Rollback

Se a ativação falhar dentro do instalador, o rollback dos artefatos gerenciados,
config/ledger/health/registros, plugin/index do runtime, units e estados dos
processos é automático. Uma falha de rollback é reportada com o diretório do
snapshot e nunca é convertida em sucesso parcial.
Para um rollback posterior:

1. desabilite e pare o timer do perfil;
2. prove que não há writer ativo;
3. preserve health JSON, ledger atual, WAL/SHM se presentes e backups;
4. selecione o venv/skill retidos e avalie compatibilidade de schema;
5. se houver necessidade de restaurar DB, pare: não há comando de restore;
   execute somente um procedimento separado, revisado e autorizado;
6. rode doctor, health, uma tentativa manual e o canário antes de reabilitar o
   timer.

Nunca apague o estado que explica a falha para “limpar”.

## 15. Desinstalação

```bash
bash installer/install.sh uninstall --dry-run
bash installer/install.sh uninstall
```

Se existir integração ativa, o uninstall default falha antes de remover a CLI.
Selecione explicitamente o alvo e o home registrados; não use
`--enable-runtime` no uninstall:

```bash
bash installer/install.sh uninstall \
  --runtime openclaw --runtime-home "$HOME/.openclaw"
```

O fluxo captura e para timer/worker, captura e para o gateway, retém
config/index/plugin e executa a desativação pelo CLI oficial. No OpenClaw,
restaura apenas os campos do plugin e as memberships `allow`/`deny` ao baseline
pré-primeira-ativação, preservando alterações não relacionadas feitas depois.
Só depois de validar config, ausência do plugin, estado do gateway e restauração
das units originais é que remove o registro de ativação, venv e skill. Qualquer
falha anterior mantém ou restaura CLI, plugin, config/index, registros, units e
estado running/stopped.

Para remover também uma cópia apenas preparada, informe o mesmo `--runtime` (e
`--runtime-home` no OpenClaw); o marcador gerenciado precisa conferir. Sem essa
seleção, o staging preparado é preservado. Units que já existiam antes da
primeira instalação são restauradas byte a byte, junto de seu estado
enabled/active original; somente units originalmente ausentes são removidas.

Config, token, ledger, `capture-health.json`, estado, backups do ledger e
backups transacionais permanecem. Remover esses dados exige inventário,
retenção e autorização separada.

## 16. Inventário real da CLI

```text
init
doctor [--allow-missing-token]
health
backup DESTINATION
route set CONVERSATION CHAT_ID THREAD_ID [--allow-update]
route list [--enabled-only] [--show-identifiers]
route blocked-list [--state blocked_no_route|requeued] [--limit N]
route reconcile CONVERSATION [--limit N]
route verify-destination CHAT_ID [--thread-id N]
route provision-topic CONVERSATION CHAT_ID TOPIC_NAME --confirm-create [--allow-update]
route import-legacy runtime SOURCE [--default-chat-id ID] [--allow-update]
ingest [SOURCE|-]
worker-once [--worker-id ID] [--profile ID]
```

Use `espelho-zap --help` e o help do subcomando da versão instalada como fonte
executável. Este documento não autoriza canal real, cutover, exclusão da legacy runtime,
outbound WhatsApp nem dois writers.
