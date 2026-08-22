# Espelho WhatsApp → Telegram — correção operacional

Data de autorização: 2026-08-21 (UTC-5)
Dono: Nilton
Estado inicial: FAIL operacional com contenção fail-closed ativa.

## Escopo

Corrigir e validar a pilha existente sem novo pareamento, fórum, sessão ou writer:

- remover item semanticamente inválido da fila ativa por quarentena durável;
- tornar o contexto Hermes bidirecional, deduplicado e metadata-only para documentos;
- definir retenção bounded para mídias sem ACK sem apagar estados ambíguos;
- alinhar CLI, worker, observer e plugin em uma geração canônica;
- reconciliar `dead`, `uncertain` e `blocked_no_route` sem replay cego;
- executar reload supervisionado, canários governados e auditoria independente.

## Requisitos

- **R1 — Single writer:** exatamente um observer usa a sessão pareada e exatamente um worker consome a fila.
- **R2 — Malformed:** todo item com identidade nativa deve ser classificado; inválido permanente vai para quarentena durável e recebe ACK local somente após commit; item sem identidade permanece não-ACKado.
- **R3 — Contexto:** inbound entregue e outbound humano devem gerar uma única mensagem `observed` por identidade de plataforma no transcript do tópico correto. Documentos projetam apenas legenda/nome/MIME/tamanho/hash.
- **R4 — Segurança:** fórum continua sem LLM automático, sem DM/Geral fallback e sem outbound autônomo; ARM e fingerprint continuam fail-closed.
- **R5 — Mídia:** após ACK confirmado, bytes expiram em até 48h; estados sem ACK têm política bounded, mas `uncertain` não é apagado antes de reconciliação.
- **R6 — Geração:** CLI, worker, observer e plugin devem pertencer à mesma release e passar o upgrade guard.
- **R7 — Reconciliação:** `uncertain` nunca sofre retry automático; `dead` e `blocked_no_route` só mudam com evidência e escopo explícito.
- **R8 — Prova viva:** testes, restart/recovery, canário inbound, outbound e mídia, contexto nos dois sentidos, dedupe, tópico correto e ausência de fallback.
- **R9 — Fechamento:** auditor independente deve retornar PASS no estado final.

## Critérios de aceite

1. Worker executa ciclos sem o `malformed=1` permanente.
2. Backfill/context projection é idempotente e não dispara LLM nem transporte.
3. Novo inbound aparece no tópico e no transcript observado exatamente uma vez.
4. Novo outbound humano chega ao WhatsApp e ao transcript exatamente uma vez.
5. Mídia usa rota correta, metadados corretos e retenção conforme estado.
6. Upgrade guard passa; ARM é recriado somente após todas as verificações.
7. Ledgers passam `quick_check` e `foreign_key_check`; nenhum replay indevido.
8. Rollback restaurável e evidenciado.
