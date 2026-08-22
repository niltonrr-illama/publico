# STATE

## Decisions

- AD-001: Evoluir a pilha existente; não criar segundo observer, sessão, fórum ou writer.
- AD-002: A geração canônica de partida é o repositório `publico` v0.3.4; a release corretiva será única para CLI, worker, observer e plugin.
- AD-003: `uncertain` nunca é reenviado nem apagado sem reconciliação independente.
- AD-004: Contexto inbound é projeção observada no tópico, não turno de agente nem memória curada.
- AD-005: ARM permanece ausente até upgrade guard, single-writer e canários locais passarem.

## Handoff

Feature: espelho-zap-operational-closure
Phase: Execute
Task: T1 — Freeze e rollback
Completed: auditoria read-only e autorização do owner
In progress: backup consistente e baseline
Next: executar restore probe e commit T1
Blockers: nenhum conhecido
Branch: fix/espelho-zap-operational-closure-20260821
