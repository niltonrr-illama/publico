# Runbook — preparar e migrar o Espelho Zap

**ID:** RUNBOOK-WHATSAPP-COCKPIT-MIGRATION-001
**Status:** ativo para preparação; cutover bloqueado até os gates passarem
**Dono:** Operator
**Executor técnico:** Maintainer ou agente explicitamente autorizado
**Pai:** `../README.md`
**Sensibilidade:** L1; bundles reais são L3-L5 e ficam fora do Git
**Última revisão:** 2026-07-22

## 1. Semântica das ordens humanas

### “Prepare o Espelho Zap para migração”

Executar sem nova entrevista:

1. ler `README.md`, `MAPA.md` e este runbook;
2. inventariar adaptadores, ledger, cursores, arquivo legado e memória curada;
3. gerar snapshot consistente e manifesto sem segredo;
4. verificar hashes e integridade SQLite;
5. fazer restore drill isolado;
6. produzir readiness com bloqueios;
7. manter origem ativa e não reconectar canais.

Essa ordem não autoriza:

- parar o runtime;
- cortar WhatsApp/Telegram;
- apagar dados;
- mudar credenciais;
- declarar migração concluída.

### “Migre o Espelho Zap para Hermes/outro”

Além da preparação:

1. confirmar alvo e janela;
2. instalar código e adaptador no alvo;
3. importar bundle em isolamento;
4. reconstruir índices derivados;
5. pedir autenticação humana apenas para canais que exigirem;
6. executar canários de captura, espelho, deduplicação e memória;
7. manter origem como autoridade até ordem explícita de cutover.

### “Faça o cutover”

Só executar quando `manifest.json` indicar `cutover_ready=true` e Operator autorizar o
switch. Registrar hora, checkpoints de origem/alvo e janela de rollback.

### “Faça rollback”

1. interromper somente o adaptador alvo;
2. devolver autoridade à origem preservada;
3. impedir replay outbound;
4. reconciliar cursores e eventos sem conteúdo em logs;
5. registrar resultado e causa;
6. preservar bundle, logs sanitizados e evidência do teste.

## 2. Inventário mínimo

| Papel | Obrigatório | Regra |
| --- | --- | --- |
| Ledger/cursosres V2 | Sim | snapshot SQLite consistente |
| Arquivo bruto legado | Sim enquanto houver lacuna de curadoria | backup criptografado e inventariado |
| Claims/evidências | Sim se existirem | exportar com escopo e supersession |
| Configuração | Sim | somente shape/IDs opacos; sem segredo |
| Código/adaptadores | Sim | commit/tag/hash |
| Daily Notes | Não | reconstruível |
| Embeddings/GBrain | Não | reconstruir no alvo |
| Sessões ativas | Conforme dependência | nunca excluir durante migração |

## 3. Preparar o bundle

1. Copiar `migration/request.example.json` para área privada.
2. Substituir caminhos fictícios pelos artefatos reais.
3. Registrar cada capability com `status` e evidência curta.
4. Executar:

```text
python migration/portable_bundle.py prepare --request <request-privado.json> --output <bundle-novo>
python migration/portable_bundle.py verify --bundle <bundle-novo>
```

Proteções do utilitário:

- recusa sobrescrever output;
- recusa symlink e path traversal;
- usa backup SQLite online;
- detecta arquivo mudando durante cópia;
- não grava caminhos de origem no manifesto;
- não imprime conteúdo;
- marca blockers automaticamente.

O bundle real deve ficar em volume criptografado ou arquivo criptografado. O utilitário
protege consistência e metadados; não substitui criptografia em repouso.

## 4. Restore drill

Em diretório isolado:

1. verificar o bundle;
2. abrir o SQLite restaurado em modo local;
3. executar `PRAGMA quick_check`;
4. comparar somente contagens e hashes com a origem;
5. validar cursores monotônicos;
6. iniciar adaptador alvo sem canais externos;
7. confirmar que replay não cria eventos duplicados;
8. registrar `restore_drill=validated` apenas depois disso.

Não usar o ambiente de produção alvo como primeiro restore drill.

## 5. Canários separados

### Captura

- uma mensagem real elegível gera exatamente um novo evento;
- o cursor avança;
- nenhum outbound é possível pelo processo de captura.

### Espelho

- uma mensagem real aparece uma vez no tópico correto;
- foto aparece como mídia original, com somente a legenda original;
- o adaptador não chama vision/OCR automático para a entrada do inbox WhatsApp;
- nenhum bloco `Description:` gerado pelo runtime aparece no Telegram;
- áudio original reproduz normalmente e sua transcrição permanece interna;
- mídia original é fail-soft em relação a OCR/transcrição;
- nenhum auto-reply é enviado;
- o teste atual só muda de `failed` para `validated` após evidência real.

Para OpenClaw, aplicar e validar o contrato de
`../mirror/openclaw/openclaw-whatsapp-inbox-image-scope.patch.json`. A negação deve
ser limitada a `agent:whatsapp-inbox:whatsapp:`; não desligar visão globalmente.
Antes de escrever configuração, executar `openclaw config patch --dry-run`, guardar
backup e rollback, e validar a configuração. O saneamento de `Description:` é
contenção secundária, não substitui o bloqueio da análise na origem.

### Curadoria e segundo cérebro

- lote limitado;
- claim inclui evidência;
- escopo não é enfraquecido;
- owner-private não é publicado em repo operacional;
- falha do LLM deixa evento intacto e não curado.

### Índices

- busca textual/vetorial é reconstruída a partir de projeções permitidas;
- falha de provider/embedding não altera ledger nem claims;
- OpenAI OAuth conversacional não é tratado como chave de embeddings.

## 6. Cutover

Pré-condições:

- todos os gates obrigatórios `validated`;
- bundle e restore drill válidos;
- autenticação do alvo concluída;
- origem congelada apenas na janela final;
- checkpoints finais reconciliados;
- canários reais no alvo aprovados;
- rollback testado.

Sequência:

1. pausar ingestão na origem sem apagar estado;
2. tirar checkpoint final;
3. importar delta;
4. subir captura no alvo;
5. validar captura;
6. subir espelho no alvo;
7. validar entrega única;
8. liberar curadoria/indexação;
9. observar;
10. só depois retirar autoridade da origem.

## 7. Retenção e exclusão

Migração aceita não autoriza apagar imediatamente o legado. A exclusão precisa de:

- categoria nomeada;
- dois backups para material insubstituível;
- manifest/hashes;
- restore testado;
- dependência descartada;
- autorização separada;
- observação pós-exclusão.

## Roteamento / Próximo passo

- arquitetura e estado -> `../README.md`
- contrato de memória -> deployment-specific memory (not included)
- operação atual do espelho -> `../../../docs/WHATSAPP_ESPELHO_OPERACAO_SEGURA.md`
- incidente real -> the private incident log

## Confirmação de escopo

Este runbook prepara, verifica, migra, corta e reverte o domínio por fases.
Ele não contém credenciais, caminhos reais de produção ou autorização de exclusão.
Fonte canônica superior: `../README.md`.
