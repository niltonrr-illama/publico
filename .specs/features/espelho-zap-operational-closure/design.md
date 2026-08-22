# Design — fechamento operacional do Espelho Zap

## Arquitetura preservada

`Baileys observer → append-only spool → portable worker/ledger → Telegram forum`

`Telegram forum human message → Hermes pre-dispatch hook → outbound ledger → narrow loopback bridge → WhatsApp`

## Alterações

1. **Normalização/quarentena:** ampliar a classificação do adapter 0.3.4 para o item vivo conhecido, mantendo a regra de identidade obrigatória.
2. **Projeção de contexto:** consumidor local idempotente escreve no `SessionStore`/`state.db` sem criar turno de agente. Inbound usa a identidade do evento e tópico da delivery/route; outbound preserva o comportamento existente.
3. **Retenção:** política explícita por estado. `sent` segue 48h; `dead` pode expirar após prazo maior e auditoria; `uncertain` fica preservado até reconciliação.
4. **Deploy:** construir uma release única, instalar lado a lado, validar, pausar timers, aguardar idle, trocar paths, recarregar e provar single-writer.
5. **Reconciliation:** produzir inventário e decisões sem replay; requeue apenas quando ausência de efeito remoto estiver provada.

## Rollback

- backup consistente dos dois ledgers e `state.db` com WAL/SHM;
- cópia de `.env`, `config.yaml`, plugin, route-map e units;
- release anterior mantida em `/opt/espelho-zap/releases/`;
- remover ARM antes de qualquer troca;
- rollback restaura config/plugin/paths e reinicia serialmente observer→worker→guard.
