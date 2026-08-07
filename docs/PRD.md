# PRD — Espelho Zap Portable

**ID:** PRD-ESPELHO-ZAP-PORTABLE-001
**Tipo:** prd
**Status:** produto definido; implementacao e aceite em andamento
**Dono:** Operator
**Escopo:** WhatsApp Cockpit
**Fonte canonica:** sim, para visao de produto
**Fonte superior:** `../README.md`
**Pai:** `PRJ-WHATSAPP-COCKPIT-PORTABLE`
**Filhos:** `ARCHITECTURE.md`, `THREAT_MODEL.md`, `INSTALLATION.md`, `ADAPTER_CONTRACT.md`, `ACCEPTANCE_TESTS.md`
**Relacionados:** the product specification and release history in this repository
**Substitui:** nenhum; consolida sem apagar documentos anteriores
**Substituido por:** nenhum
**Sensibilidade:** L1; distribuicao nao inclui dados reais
**Ultima revisao:** 2026-08-04

## Resumo executivo

Espelho Zap Portable e um produto instalavel que transforma eventos inbound de um WhatsApp ja pareado em quatro resultados independentes:

1. espelho visual por conversa em topicos de um grupo Telegram;
2. ledger canonico multicanal;
3. Daily Notes e claims com evidencia;
4. projecoes de busca/GBrain reconstruiveis.

O produto nao e um bot que responde espontaneamente no WhatsApp. Ele observa,
preserva, organiza e projeta; tambem transporta de volta a resposta que o
operador humano escreve no topico mapeado. O nucleo roda fora da LLM, usa regras
deterministicas e pode ser hospedado por OpenClaw, Hermes ou outro runtime via
adapter.

## Por que virou produto

O sistema legacy runtime ja possuia captura, roteamento e dedupe valiosos. Na migracao, a ausencia de um contrato executavel permitiu uma versao que enviava trafego para a DM do agente. O problema nao era falta de uma frase sobre “Telegram”; faltavam artefatos que impedissem uma interpretacao errada:

- schema de rota;
- invariantes de control plane/data plane;
- estado persistente da entrega;
- testes negativos de fallback;
- instalador, adapter contract e aceite.

Uma skill sozinha nao resolve isso: skill orienta um agente, mas nao e daemon, banco nem transportador. Um MCP sozinho tambem nao: ele e interface de ferramentas, nao fila duravel. Por isso a composicao e:

| Artefato | Papel |
| --- | --- |
| distribuicao `espelho-zap-portable` | nucleo executavel e CLI |
| pacote `espelho_zap` | dominio, persistencia, rota, entrega e consumidores |
| adapters OpenClaw/Hermes | traducao da fonte do host |
| skill `espelho-zap-portable` | operacao guiada e onboarding; display “WhatsApp Context Mirror / Espelho Zap Portable” |
| MCP opcional | consultas read-only futuras |
| docs/testes/instalador | reproducao, seguranca, upgrade e rollback |

## Proposta de valor

- Reaproveita o WhatsApp ja pareado no runtime; nao exige uma segunda pilha de WhatsApp.
- Persiste antes da LLM, evitando depender de contexto de sessao/compactacao.
- Mantem uma conversa em um topico explicitamente cadastrado.
- Impede fallback silencioso para DM.
- Entrega foto/audio originais sem descricao automatica.
- Mantem conhecimento separado do indice; trocar provider nao apaga memoria.
- Pode ser instalado por outra pessoa sem paths, IDs ou segredos ExampleCo.

## Benchmark do modelo de aula

O modelo WhatsApp -> API/webhook -> n8n/Edge Function -> banco -> agente e valido como referencia de separacao entre captura, armazenamento e agente. O produto preserva essa separacao, mas nao obriga n8n, Supabase ou uma API WhatsApp externa porque o ambiente ja possui canal pareado e um ledger SQLite testado. Adapters permitem trocar a origem ou o armazenamento no futuro sem mudar o contrato de evento.

## Personas e jornadas

### Dono/gestor

1. conversa normalmente com gestores pelo WhatsApp;
2. consulta cada conversa no topico correspondente;
3. usa DM com o agente para comandos e perguntas, nunca como caixa de entrada espelhada;
4. consulta contexto curado sem depender do historico da sessao.

### Operador tecnico

1. instala artefato fixado;
2. configura adapter, paths e secrets externos;
3. importa rotas/watermark existentes em dry-run;
4. executa doctor e testes sem canal;
5. ativa um unico writer;
6. roda canario humano; monitora e reverte se necessario.

### Membro autorizado da comunidade

1. recebe codigo/pacote e documentacao sem dados ExampleCo;
2. escolhe OpenClaw ou Hermes;
3. aponta para seu canal WhatsApp ja pareado;
4. cadastra seu grupo/topicos Telegram;
5. roda seus proprios canarios.

## Requisitos de produto

### Obrigatorios no primeiro release

- eventos canonicos e idempotentes;
- SQLite versionado e migracoes aditivas namespaced;
- rota explicita por `chat_id + message_thread_id`;
- outbox duplicate-resistant com estado `uncertain`;
- Telegram text/photo/voice/audio/document conforme contrato;
- outbound humano natural no tópico mapeado para texto e mídias suportadas;
- zero outbound WhatsApp automático, de LLM ou de bot;
- lease single-writer;
- adapter OpenClaw e Hermes sob contrato comum;
- importador legacy runtime com watermark/dedupe;
- CLI, wheel, instalacao, doctor, backup e rollback;
- skill fina e documentos completos;
- testes automaticos e aceite humano.

### Importantes depois do primeiro release

- consumidores Daily Notes/claims/search fortalecidos;
- MCP read-only;
- metricas exportaveis;
- adapters adicionais.

## Metricas e SLOs de produto

Nao existe promessa de infalibilidade. Os objetivos medidos sao:

| Metrica | Gate inicial |
| --- | --- |
| eventos duplicados no ledger em replay | 0 |
| fallback para DM | 0 chamadas |
| roteamento incorreto em fixture/canario | 0 |
| retries automaticos de `uncertain` | 0 |
| outbound WhatsApp automatico | 0 |
| outbound humano duplicado em replay | 0 |
| perda silenciosa por limite de legenda | 0 |
| SQLite quick-check | `ok` |
| testes legados removidos | 0 |
| secrets/dados ExampleCo no pacote comunitario | 0 |
| canario real | texto, foto e audio uma vez no topico correto |

Latencia, throughput e retencao devem ser medidos no host alvo antes de receber um SLO numerico; nao serao inventados neste PRD.

## Release e distribuicao

- Release pode ser wheel/checkout fixado por commit/tag.
- O core e seus artefatos portateis usam Apache-2.0.
- Publicacao exige scanner do pacote e manifest de arquivos, seja privada ou aberta.
- PyPI/ClawHub/repositorio publico sao decisoes operacionais; a licenca nao e mais bloqueio.
- Dados reais, tokens, sessoes autenticadas e manifests privados nunca acompanham a distribuicao.

## Fases

1. **Graduacao:** empacotar o core existente e completar router/outbox/delivery.
2. **Compatibilidade:** adapters, importador legacy runtime e consumidores.
3. **Operacao:** CLI, instalador, skill, upgrade/rollback.
4. **Aceite:** suites, adversarial, staging e canario real.
5. **Distribuicao:** privada ou comunitaria, sempre sem estado/dados do host.

## Riscos de produto

- APIs de runtime podem mudar: adapter fino + contract tests.
- Telegram nao oferece idempotency key: estados persistentes e bloqueio de outcome incerto.
- Midia pode encher disco: quota, retencao e purge pos-ACK.
- Curadoria pode vazar scopes: filtro deterministico antes de LLM e evidencias.
- Dois runtimes podem disputar canal: lease e cutover single-writer.
- Usuario pode esperar que “instalou” signifique “validou”: release e producao possuem gates distintos.

## Definicao de pronto

Produto pronto para um host significa codigo empacotado, instalacao limpa, configuracao validada, banco migrado/restaurado, adapter em contract tests, route importada, single-writer provado e canario humano aprovado. Um commit, um ACK, uma suite local ou “bot respondeu” isoladamente nao bastam.

## Roteamento / Proximo passo

Se voce chegou aqui procurando:
- requisitos completos -> leia `../.specs/features/portable-product/spec.md`;
- desenho tecnico -> leia `ARCHITECTURE.md`;
- instalacao -> leia `INSTALLATION.md`;
- seguranca -> leia `THREAT_MODEL.md`;
- testes -> leia `ACCEPTANCE_TESTS.md`.

## Confirmacao de escopo

Este documento trata da visao, valor, usuarios, requisitos e release do Espelho Zap Portable.
Este documento nao autoriza por si so cutover, ativacao operacional ou
publicacao aberta. Outbound humano e requisito do produto e segue os gates de
`HUMAN_OUTBOUND.md`; outbound automatico permanece proibido.
Fonte canonica superior: `../README.md`.

## Adendo canônico 0.3 — admissão seletiva e aceite real

1. Contatos diretos e grupos possuem políticas independentes. Contatos podem
   criar tópico no primeiro inbound; grupos nunca são autocriados/admitidos.
2. Grupos usam allowlist exata, isolada por perfil, rota e privacy scope.
   Eventos de grupo não aprovado são contados de forma agregada e rejeitados
   antes de persistir corpo ou mídia.
   O mesmo `group_approved` é obrigatório no outbound humano; uma rota ou
   tópico existente, isoladamente, nunca concede autoridade de envio ao grupo.
3. Um grupo aprovado inicia como `mirror_only`/`agent_mode=none`. Atuação da LLM
   só pode ser `mention_only`, depois do grill de dez respostas e canários
   positivo (com menção) e negativo (sem menção).
4. Identidade do participante é um diretório privado separado do rótulo
   visível. IDs crus nunca são renderizados; identidade não resolvida bloqueia
   o aceite do grupo.
5. OCR e vision são operações humanas sob demanda, fora do caminho automático.
   Transcrição de áudio é contexto interno e jamais conteúdo do espelho.
6. Retenção de mídia gerenciada é pós-ACK, limitada a 48 horas e sujeita à
   cota do spool. Arquivos mais antigos são os primeiros elegíveis à limpeza.
7. Recibos são capability-gated e monotônicos. Ausência de evento do provider
   permanece “não comprovado”, nunca “lido” inferido.
8. `prepared` não é instalação concluída. O produto só usa
   `installed_success` depois da matriz humana inbound/outbound para texto,
   imagem e áudio.
