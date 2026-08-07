# Outbound humano do Espelho Zap

## Recibos de entrega 0.3.3

O envio humano registra `sent`. Quando o runtime pareado publicar receipt, o
adapter traduz somente fatos observados: `2=sent/device`, `3=delivered`,
`4=read` e `5=played`. Atualizações atrasadas nunca rebaixam o estado e ausência
de evento nunca é tratada como leitura. A UI pode atualizar um indicador ou
reação existente, sem criar uma nova mensagem para cada receipt.

No Hermes/Baileys, os eventos `message-receipt.update` e `messages.update` são
convertidos em envelopes `outbound_receipt` e passam pelo mesmo ACK durável do
spool. O tique visual do aplicativo WhatsApp continua sendo responsabilidade
do próprio provedor; o produto registra o estado observado para contexto,
auditoria e projeções sem fabricar um segundo tique.

Quando `ESPELHO_ZAP_RECEIPT_REACTIONS=enabled`, o produto aplica somente uma
reacao do bot na mensagem Telegram original: `✅` para `delivered` e `👀`
para `read`/`played`. A mensagem original nao e editada, nenhuma
resposta textual e criada e uma falha do Telegram deixa o receipt duravel para
nova tentativa no proximo ciclo.

## Resultado esperado

O espelho e bidirecional para o operador humano autorizado:

```text
WhatsApp -> topico Telegram        (inbound espelhado)
topico Telegram -> WhatsApp        (outbound humano)
```

No topico ja mapeado de uma conversa individual ou grupo, uma mensagem escrita ou anexada por um
operador autorizado e uma intencao explicita de responder aquela conversa. Nao
ha comando `/wa`, escolha de numero, aprovacao extra ou passagem pela LLM.

Esse comportamento reproduz a experiencia da legacy runtime. A implementacao portatil
e mais restrita na autoridade e mais robusta na persistencia: ela recebe o
evento Telegram diretamente no hook oficial anterior ao agente, reserva a
intencao pelo ID da mensagem Telegram e usa uma unica fila de envio WIP=1.

## Tipos suportados

- texto, preservado literalmente;
- foto/imagem, com somente a legenda humana;
- voice note e audio;
- video;
- documento, com nome e legenda quando presentes.

Uma copia gerenciada da midia pode existir enquanto o envio esta pendente. Ela
e removida depois da confirmacao de envio. Midia em estado `uncertain` e
preservada para conciliacao e nunca reenviada cegamente.

## Autoridade deterministica

Um evento so pode virar outbound quando todas as condicoes abaixo forem
verdadeiras:

1. plataforma `telegram`;
2. `chat_id` e exatamente o forum configurado;
3. `thread_id` resolve para uma unica rota ativa, com destino explicito de contato ou grupo;
4. autor pertence a `allowed_users` e nao e bot;
5. existe `message_id` Telegram estavel;
6. corpo contem texto ou uma midia suportada;
7. o transporte e exclusivamente o canal WhatsApp ja pareado do runtime:
   adapter nativo no OpenClaw ou endpoint loopback autenticado no Hermes.
8. no Hermes, existe um ARM privado valido, ligado ao commit de release, ao
   SHA-256 exato do plugin e ao fingerprint do runtime carregado.

DM, outro grupo, topico desconhecido, bot, assistant, automacao, mensagem de
servico ou replay nao sao enviados ao WhatsApp. O forum continua fora da LLM:
depois de capturar ou rejeitar o evento, o hook retorna `skip`.

Cada item de midia aceita no maximo 128 MiB e o conjunto da mensagem aceita no
maximo 256 MiB. O limite vale na leitura da origem e durante/depois da copia
gerenciada, protegendo tambem contra crescimento do arquivo entre as etapas.

## Persistencia e dedupe

- chave primaria logica: `telegram:<chat_id>:<thread_id>:<message_id>`;
- reserva duravel acontece antes da chamada ao transporte WhatsApp;
- segunda observacao da mesma chave nao reenvia;
- um unico dispatcher envia por vez;
- sucesso grava o ID remoto do WhatsApp;
- rejeicao comprovada pode virar falha;
- timeout, excecao depois do inicio do envio ou resultado ambiguo vira
  `uncertain`, sem retry automatico;
- IDs remotos do WhatsApp ficam registrados e podem alimentar supressao de eco.

O ledger privado preserva a trilha operacional independentemente da sessao da
LLM. Projecao para contexto, Daily Notes ou claims e um consumidor separado;
reinicio e compactacao nao apagam o registro de entrega.

## ARM persistente no Hermes

Habilitar as variaveis nao basta para autorizar envio. O plugin sempre pode
inicializar o ledger e publicar seu marcador de startup, mas so aceita,
prepara ou drena outbound quando `ESPELHO_ZAP_HUMAN_OUTBOUND_ARM_FILE` aponta
para um arquivo privado regular, sem symlink, com modo `0600` no POSIX e este
contrato exato:

```json
{"schema_version":1,"release_commit":"<40hex>","plugin_sha256":"<64hex>","hermes_runtime_fingerprint":"<64hex>","armed":true}
```

`release_commit` deve coincidir com `ESPELHO_ZAP_RELEASE_COMMIT`; o hash deve
coincidir com os bytes do `integrations/hermes/__init__.py` efetivamente
carregado; e `hermes_runtime_fingerprint` deve coincidir com o marker do
 processo atual. O fingerprint cobre executavel/prefixo/`pyvenv.cfg`, metadados
 do pacote e manifestos deterministas e limitados de todo o codigo Python dos
 pacotes obrigatorios `gateway` e `hermes_cli`, sem importar modulos para
 descobri-los. Componente obrigatorio ausente invalida o fingerprint. O gate e
 relido antes de cada evento, antes da reserva duravel,
antes de cada claim e imediatamente antes da chamada HTTP. Ausencia e estado
desarmado normal; arquivo permissivo, malformado ou divergente e bloqueio
fail-closed.

Se o ARM desaparecer com um item ainda `prepared`, ele permanece intacto e
sem consumir tentativa. Se desaparecer depois do claim, mas antes da ultima
validacao imediatamente anterior a `connection.request`, o item volta para
`prepared`, tambem sem consumir tentativa. A remocao do arquivo nao e um
cancelamento linearizavel de uma syscall ja em voo: depois da validacao final,
o resultado e fechado como `sent` ou `uncertain` para impedir duplicacao. Um
 restart normal com ARM valido recupera o ledger e drena; sem ARM, apenas
 carrega e permanece inerte. Recriar o ARM habilita novos eventos; um watcher
 local dedicado acorda automaticamente a fila `prepared` preexistente, sem
 depender de nova mensagem e sem consumir tentativa enquanto desarmado. O deploy recusa fila
`prepared`, remove o ARM durante preflight e so o recria atomicamente depois
de validar commit, hashes de plugin/runtime, configuracao e single-writer.

## Transporte por runtime

No Hermes, o bridge permanece em `observe-only` para todas as rotas genericas. `/send`,
`/send-media`, typing, auto-reply e qualquer caminho de agente continuam
bloqueados. Somente os endpoints loopback autenticados do espelho humano podem
usar a fila serializada da sessao WhatsApp ja pareada:

- `POST /mirror-human-send`, com payload tipado para texto e/ou mídia.

O endpoint de midia confere raiz permitida, arquivo regular sem symlink,
tamanho, SHA-256 e tipo antes de ler o arquivo. Nenhum segundo bridge, segundo
pareamento ou runtime do PR #187 deve ser ativado.

No OpenClaw 2026.7.1 ou superior, nao existe endpoint adicional. O plugin
carrega `api.runtime.channel.outbound.loadAdapter("whatsapp")` e chama
`sendText` ou `sendMedia` na conta ja pareada. `audioAsVoice` preserva voice
note e `forceDocument` preserva documento. Os hooks de resposta automatica
continuam cancelados; essa chamada e uma intencao humana allowlisted e nao uma
resposta de agente.

## Diferenca para a legacy runtime

| Aspecto | legacy runtime | Produto portatil |
| --- | --- | --- |
| Experiencia | mensagem humana normal no topico | identica |
| Tipos comprovados | texto, imagem, audio e documento | texto, imagem, voz/audio, video e documento |
| Captura | varredura de JSONL | hook Telegram pre-agent do Hermes ou `message_received` do OpenClaw |
| Latencia | worker de ate um minuto | despacho local imediato e serializado |
| Dedupe | ID + fingerprint de conteudo | ID Telegram; texto identico novo continua valido |
| Fila | misturava manual e automacoes | lane exclusiva de outbound humano, WIP=1 |
| Falha ambigua | bloqueava replay | mantida como `uncertain`, sem duplicar |
| Destino | topico e comandos legados em DM | somente contato/grupo da rota exata do topico |

Portanto, o comportamento externo e o mesmo; a implementacao interna e
otimizada e elimina os atalhos legados de DM, numero arbitrario e worker
compartilhado.
