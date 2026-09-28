# Pocketful — Stage 1

## Build and run

```bash
cd stage-1
docker build -t pocketful-s1 .
docker run -p 8080:8080 pocketful-s1
```

## Verify

```bash
curl http://localhost:8080/health
# → {"status":"ok"}
```

## Test reset

```bash
curl -X POST http://localhost:8080/_test/reset \
  -H "Content-Type: application/json" \
  -d '{"currency":"EUR","minor_units":2,"users":[{"id":"u_ada","email":"ada@example.com","password":"correct horse","display_name":"Ada","handle":"ada","balance":10000}]}'
# → 204 No Content
```
