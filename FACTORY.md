# Pocketful — Dark Factory

## Team composition

| Seat | Role | Model |
|------|------|-------|
| planner-rwhp | Architect/Planner | Claude |
| lisa | Coordinator + Executor | MiMo Pro |
| gwen | Executor (local inference) | Local LLM |
| sigyn | Executor | MiMo Pro |

## Factory design

**Architecture:** Coordinator (LISA) receives the task, validates the plan from Claude, decomposes into atomic subtasks, and delegates to Gwen and Sigyn in parallel. Each subagent gets a self-contained goal with exact file paths, function signatures, and acceptance criteria. LISA verifies every output before marking it complete.

**Communication:** All inter-agent communication flows through Band. LISA is the single bus — agents never talk directly to each other. This prevents misalignment and ensures every handoff is verified.

**Verification:** Every step has explicit acceptance criteria. LISA runs tests after each delegation. No step opens until its dependencies are green. The invariant test harness (57+ tests) runs after every integration.

**Cost optimization:** Gwen runs on local inference (free), handling all API endpoint work. Sigyn handles data layer. Claude provides architecture. LISA coordinates and verifies.

## Design choices

1. **Double-entry ledger:** Every money movement creates two balanced ledger entries. The source of truth is the ledger; wallet balances are cached denormalizations.
2. **Idempotency keys:** Scoped per user. Same key + same body = replay (200). Same key + different body = 409. Keys are reusable after failed attempts.
3. **Ascending ID locks:** All transfers lock accounts in ascending order to prevent deadlocks under concurrent A→B and B→A.
4. **SQLite WAL mode:** Sufficient for hackathon load (50 concurrent requests). No external dependencies.

## Measured resources

- Model tokens: ~50K (planning + verification)
- Gwen: local inference, $0
- Build time: ~2 hours for Stage 1

## Failure recovery

- Every delegation is verified before acceptance
- Failed subtasks are re-dispatched with corrected context
- Invariant tests run after every integration checkpoint


## 📊 Budgets

See [budgets.json](./budgets.json) for hard cost caps per role.

| Role | Model | Cap | Spent |
|------|-------|-----|-------|
| Architect/Planner | Claude | $2.00 | $0.85 |
| Coordinator | MiMo Pro | $1.50 | $0.45 |
| Executor (heavy) | Qwen 80B local | $1.50 | $0.30 |
| Executor (light) | MiMo Pro | $1.50 | $0.25 |
| **Total** | | **$6.50** | **$1.85** |

**Hard cap behavior:** Hitting cap locks the role. Escalation requires authorizer_user_id.

## ⏰ Autoscheduler

See [scheduler.json](./scheduler.json).

- **Enabled:** Yes
- **Reinforcement:** Off
- **Schedule:** Every 30 minutes
- **Autonomy per turn:** 3 actions
- **Turns per stage:** 10 max
- **Timeout:** 300s
- **Retries on failure:** 2

## 🔄 Session Recovery

See [RECOVERY.md](./RECOVERY.md) for the full session-end recovery report.

**Summary:** 4 stages complete, 82 tests passing, $1.85 spent, 3× 503 failures recovered via fallback execution.

## 📝 Entry URLs

The submission entry URLs are maintained by lablab.ai upon submission. This repository contains all code, evidence, and documentation required for evaluation.
