# Pocketful — Dark Factory Hackathon

**Track:** Pocketful · **Event:** WeAreDevelopers x BAND — Dark Factory (lablab.ai)

A wallet and payments app built by an autonomous agent team in Band Desktop.

## Quick Start

```bash
cd stage-1
docker build -t pocketful-s1 .
docker run -p 8080:8080 pocketful-s1
```

Verify:
```bash
curl http://localhost:8080/health
# → {"status":"ok"}
```

## Team

| Seat | Role | Model |
|------|------|-------|
| planner-rwhp | Architect/Planner | Claude |
| lisa | Coordinator + Executor | MiMo Pro |
| gwen | Executor (local inference) | Qwen 80B (local) |
| sigyn | Executor | MiMo Pro |

## Architecture

- **Double-entry ledger:** Every payment creates two balanced ledger entries that always sum to zero
- **Idempotency:** Scoped per user, reusable after 4xx failures
- **Ascending ID locks:** Prevents deadlocks under concurrent A→B and B→A transfers
- **SQLite WAL:** Zero external dependencies, sufficient for 50 concurrent requests

## Evidence

- [FACTORY.md](FACTORY.md) — Factory design, costs, and failure recovery
- [mandates/](mandates/) — Generic seat instructions
- [spec/](spec/) — Track specifications (stages 1-4)
- [stage-1/](stage-1/) — Standalone stage 1 with Dockerfile

## License

MIT