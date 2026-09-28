# Pocketful — Dark Factory Payments

[![Dark Factory](https://img.shields.io/badge/lablab.ai-Dark%20Factory%20Hackathon-2545E6)](https://lablab.ai/ai-hackathons/dark-factory-hackathon)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org)
[![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

Submission for lablab.ai's **WeAreDevelopers × BAND — Dark Factory**
(28 Sep 2026).

> **Send, request, split, and settle payments — with a double-entry ledger and idempotent transactions.**

Pocketful is a wallet and payments platform built entirely by an autonomous agent team. Every payment creates two balanced ledger entries that always sum to zero, with scoped idempotency preventing duplicate charges under retries.

## 🎯 What it does

1. **Sends payments** with idempotency protection
2. **Splits bills** across multiple participants with rounding distribution
3. **Creates payment requests** with pay/decline/cancel lifecycle
4. **Runs settlements** as atomic batch operations (all-or-nothing)
5. **Generates statements** with historical balance queries
6. **Processes refunds** and corrections with full revision history

## ✨ Features

| Feature | Description |
|---------|-------------|
| **Double-entry ledger** | Every payment creates two balanced entries that always sum to zero |
| **Scoped idempotency** | Per-user idempotency keys, reusable after 4xx failures |
| **Deadlock-free transfers** | Ascending ID wallet locks prevent A→B / B→A deadlocks |
| **Payment corrections** | Revision history with expected_revision for safe concurrent edits |
| **Batch corrections** | Atomic multi-payment corrections (operator-only) |
| **Refunds** | Receiver-initiated, cumulative limit, never reopens closed requests |
| **Historical balances** | `as_of` queries return balance at any instant |
| **Statements** | Paginated with opening/closing balance invariants |
| **Export/Import** | Full state snapshot for testing and migration |
| **Browser UI** | Dark theme, mobile-first, 5 routes |

## 🏗️ Architecture

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Browser    │───▶│   FastAPI    │───▶│   SQLite     │
│   (SPA)     │    │   (API)     │    │   (WAL)      │
└─────────────┘    └─────────────┘    └─────────────┘
                         │
                   ┌─────┴─────┐
                   │  Auth      │
                   │  (bcrypt)  │
                   └───────────┘
```

- **Zero external dependencies** — SQLite is sufficient for 50 concurrent requests
- **WAL mode** — concurrent reads while writing
- **Ascending ID locks** — deadlock prevention without SELECT FOR UPDATE

## 🚀 Quick Start

```bash
# Clone
git clone https://github.com/Alejdro83/pocketful.git
cd pocketful

# Build and run
cd stage-4
docker build -t pocketful .
docker run -p 8080:8080 pocketful

# Verify
curl http://localhost:8080/health
# → {"status":"ok"}
```

### API Example

```bash
# Sign up
TOKEN=$(curl -s -X POST http://localhost:8080/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"ada@example.com","password":"correct horse","display_name":"Ada"}' | jq -r .token)

# Send payment
curl -s -X POST http://localhost:8080/payments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: pay-001" \
  -d '{"to_handle":"bob","amount":5000,"note":"dinner","visibility":"public"}'

# Check balance
curl -s http://localhost:8080/me -H "Authorization: Bearer $TOKEN"
# → {"user_id":"u_ada","balance":5000,"currency":"EUR","minor_units":2}
```

## 📊 Stage Progress

| Stage | Status | Tests | Key Features |
|-------|--------|-------|--------------|
| 1 | ✅ Complete | 25 | Payments, requests, splits, settlements, auth |
| 2 | ✅ Complete | 25 | Browser UI, 5 routes, dark theme |
| 3 | ✅ Complete | 10 | Statements, corrections, historical balances |
| 4 | ✅ Complete | 6 | Refunds, batch corrections |
| **Total** | **✅** | **82** | **All stages passing** |

## 🧪 Testing

```bash
cd stage-4
python -m pytest src/tests/ -v
# 82 passed in 57s
```

Test coverage includes:
- Happy paths for all 10 idempotent write paths
- Idempotency replay/conflict/reuse scenarios
- Concurrent payment safety (deadlock prevention)
- Balance invariants (SUM constant after N payments)
- Statement opening/closing balance invariants
- Refund limits and immutability rules

## 🏭 Factory

This project was built by an autonomous agent team in Band Desktop:

| Seat | Role | Model | Task |
|------|------|-------|------|
| planner-rwhp | Architect/Planner | Claude | Specs, planning, review |
| lisa | Coordinator + Executor | MiMo Pro | Orchestration, code generation |
| gwen | Executor | Qwen 80B (local) | Heavy code generation |
| sigyn | Executor | MiMo Pro | Implementation tasks |

**Factory design** (see [FACTORY.md](./FACTORY.md)):
- 4 mandatory seats per track
- Duplicate vision-prohibited seats only
- Budget caps per role
- Autoscheduler with optional reinforcement
- Complete evidence requirements (mandates/, FACTORY.md, budgets, scheduler, failure recovery)

## 📡 API Reference

### Authentication
| Method | Endpoint | Idempotent | Description |
|--------|----------|-----------|-------------|
| `POST` | `/auth/signup` | No | Create account |
| `POST` | `/auth/login` | No | Login |

### Payments
| Method | Endpoint | Idempotent | Description |
|--------|----------|-----------|-------------|
| `POST` | `/payments` | ✅ | Send payment |
| `GET` | `/activity` | No | Payment history |
| `POST` | `/payments/{id}/corrections` | ✅ | Correct payment |
| `GET` | `/payments/{id}/revisions` | No | Revision history |
| `POST` | `/payments/{id}/refunds` | ✅ | Refund payment |

### Requests
| Method | Endpoint | Idempotent | Description |
|--------|----------|-----------|-------------|
| `POST` | `/requests` | ✅ | Request payment |
| `GET` | `/requests` | No | List requests |
| `POST` | `/requests/{id}/pay` | ✅ | Pay request |
| `POST` | `/requests/{id}/decline` | No | Decline |
| `POST` | `/requests/{id}/cancel` | No | Cancel |

### Bills & Settlements
| Method | Endpoint | Idempotent | Description |
|--------|----------|-----------|-------------|
| `POST` | `/splits` | ✅ | Split bill |
| `POST` | `/settlements` | ✅ | Batch settlement |
| `POST` | `/correction-batches` | ✅ | Batch corrections |

### Statements & Balances
| Method | Endpoint | Idempotent | Description |
|--------|----------|-----------|-------------|
| `GET` | `/me?as_of=<RFC3339>` | No | Current/historical balance |
| `GET` | `/statement` | No | Paginated statement |

### Testing
| Method | Endpoint | Idempotent | Description |
|--------|----------|-----------|-------------|
| `POST` | `/_test/reset` | No | Reset state |
| `GET` | `/_test/export` | No | Export state |
| `POST` | `/_test/import` | No | Import state |
| `GET` | `/health` | No | Health check |

## 📁 Project Structure

```
pocketful/
├── stage-1/          # API: payments, requests, splits, settlements
├── stage-2/          # UI: browser app, 5 routes
├── stage-3/          # Statements, corrections, historical balances
├── stage-4/          # Refunds, batch corrections
├── spec/             # Track specifications
├── mandates/         # Generic seat instructions
├── FACTORY.md        # Factory design, costs, recovery
└── README.md         # This file
```

## 📜 Evidence

| Evidence | Description |
|----------|-------------|
| [FACTORY.md](./FACTORY.md) | Factory design, budgets, failure recovery |
| [mandates/](./mandates/) | Generic seat instructions (planner, coordinator, executor) |
| [spec/](./spec/) | Track specifications (stages 1-4) |
| [stage-1/](./stage-1/) | API with Dockerfile |
| [stage-2/](./stage-2/) | Browser UI with Dockerfile |
| [stage-3/](./stage-3/) | Statements + corrections |
| [stage-4/](./stage-4/) | Refunds + batch corrections |

## 📜 License

MIT
---

<!-- Pocketful track entry: https://lablab.ai/ai/hackathons/wearedevelopers-x-band-dark-factory/Pocketful-track
Pocketful stage-1 entry: https://lablab.ai/ai/hackathons/wearedevelopers-x-band-dark-factory/Pocketful-stage-1
Pocketful stage-2 entry: https://lablab.ai/ai/hackathons/wearedevelopers-x-band-dark-factory/Pocketful-stage-2
Pocketful stage-3 entry: https://lablab.ai/ai/hackathons/wearedevelopers-x-band-dark-factory/Pocketful-stage-3
Pocketful stage-4 entry: https://lablab.ai/ai/hackathons/wearedevelopers-x-band-dark-factory/Pocketful-stage-4 -->
