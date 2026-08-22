# Tasks — fechamento operacional do Espelho Zap

## Gate commands

- Quick: `python3 -m unittest discover -s tests_core -p 'test_*.py'`
- Full: `python3 -m unittest discover -s tests_core -p 'test_*.py' && python3 -m unittest discover -s tests_packaging -p 'test_*.py'`
- Build: `python3 -m compileall -q src integrations && python3 -m unittest discover -s tests_core -p 'test_*.py' && python3 -m unittest discover -s tests_packaging -p 'test_*.py'`

## T1 — Freeze e rollback

- Salvar auditoria, hashes, config sanitizada e backups SQLite consistentes.
- Verificar restore em cópias descartáveis e integridade.
- Gate: Build documental + restore probe.

## T2 — Quarentena do malformed

- Teste: evento vivo semanticamente inválido com `messageId` deve ser quarantined+ACK após commit.
- Teste negativo: evento sem identidade não é ACKado.
- Gate: Quick.

## T3 — Contexto bidirecional

- Adicionar projeção inbound observada, deduplicada por `platform_message_id`, para tópico resolvido.
- Preservar outbound e metadata-only de documentos.
- Adicionar backfill idempotente sem transporte/LLM.
- Gate: Full.

## T4 — Retenção sem ACK

- Manter `uncertain` intocado.
- Aplicar prazo explícito e auditado a `dead`/sem-delivery somente após janela definida.
- Testar arquivo fora da raiz, symlink, crash/restart e idempotência.
- Gate: Full.

## T5 — Release e deploy alinhado

- Atualizar versão, changelog, plugin e templates.
- Construir artefato, instalar lado a lado e executar preflight.
- Alinhar `.env`, worker, observer e plugin; manter ARM ausente durante troca.
- Gate: Build + upgrade guard PASS em modo check.

## T6 — Reconciliação e ativação

- Quarentenar o item inválido com evidência.
- Inventariar e reconciliar `dead`, `uncertain` e bloqueios sem replay cego.
- Drain, reload serial, single-writer, rollback drill e rearm.
- Gate: live activation checklist.

## T7 — Canários e auditoria final

- Canários inbound texto/mídia e outbound texto/mídia no escopo mapeado.
- Provar tópico, ACK/receipt, contexto, dedupe, sem DM/Geral e sem resposta automática.
- Auditor independente PASS e relatório final.
- Gate: end-to-end.
