# Configuration reference

All settings use the `RECALL_` prefix. Legacy `REMEMBRANCE_` names remain temporary fallbacks.

| Variable | Default | Meaning |
| --- | --- | --- |
| RECALL_HOME | ~/.recall | Runtime data root |
| RECALL_DB_PATH | HOME/memory.db | Primary SQLite database |
| RECALL_HOST / RECALL_PORT | 127.0.0.1 / 8788 | REST bind |
| RECALL_API_TOKEN_FILE | unset | Current bearer token file |
| RECALL_PREVIOUS_API_TOKEN_FILE | unset | Previous-token overlap file |
| RECALL_ALLOWED_ORIGINS | empty | Exact comma-separated CORS origins |
| RECALL_MAX_BODY_BYTES | 1048576 | Maximum HTTP body |
| RECALL_MAX_CAPTURE_CHARS | 262144 | Maximum captured text |
| RECALL_MAX_RESULTS | 100 | Maximum result limit |
| RECALL_MAX_GRAPH_DEPTH | 4 | Maximum graph traversal |
| RECALL_READS_PER_MINUTE | 120 | Per-client read limit |
| RECALL_WRITES_PER_MINUTE | 30 | Per-client write limit |
| RECALL_CAPTURE_PROCESSING_TIMEOUT | 15 | Seconds before capture returns pending |
| RECALL_PROCESSING_QUEUE_LIMIT | 64 | Maximum in-flight and queued capture jobs |
| RECALL_OLLAMA_URL | http://127.0.0.1:11434 | Ollama API |
| RECALL_EXTRACT_MODEL | nemotron-3-nano:4b | Extraction model |
| RECALL_EMBED_MODEL | embeddinggemma | Embedding model |
| RECALL_EMBEDDINGS_ENABLED | false | Enable capture/query embeddings; production sets true |
| RECALL_OLLAMA_MAX_CONCURRENCY | 2 | Ollama worker limit |
| RECALL_NATS_URL | nats://127.0.0.1:4222 | NATS connection |
| RECALL_NATS_CREDS_FILE | unset | NATS user credentials file |
| RECALL_JSON_LOGS | false | Emit structured service logs |
| RECALL_NATS_SUBJECT | *.agent.output | Capture subject |
| RECALL_NATS_STREAM | RECALL_AGENT_OUTPUT | JetStream name |
| RECALL_NATS_CONSUMER | recall-capture-v1 | Durable consumer |
| RECALL_NATS_DLQ_SUBJECT | recall.agent.output.dlq | Dead-letter subject |

A non-loopback `RECALL_HOST` requires a configured token. Container deployments use a non-loopback container bind but publish only to host loopback.
