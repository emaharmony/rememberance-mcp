# REST API examples

When authentication is configured, add `Authorization: Bearer TOKEN` to every route except liveness.

```bash
curl http://127.0.0.1:8788/health/live
curl -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Recall uses SQLite","project":"recall","agent":"codex"}' \
  http://127.0.0.1:8788/capture
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8788/search?q=SQLite&mode=balanced&project=recall"
curl -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task":"deploy Recall","project":"recall","agent":"codex"}' \
  http://127.0.0.1:8788/context/build
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8788/memories/MEMORY_ID
```

Capture responses include `processing_status` and `embedding_status`. Search modes are `keyword`, `vector`, `balanced`, and `deep`.
