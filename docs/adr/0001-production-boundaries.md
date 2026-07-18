# Architecture decisions

## Private Tailscale boundary

Recall uses Tailscale Serve instead of direct public exposure. Services remain host-loopback only, and bearer auth provides defense in depth.

## SQLite and exact vectors

A single SQLite database remains the authoritative store. Exact cosine retrieval is the compatibility path; sqlite-vec 0.1.9 is pinned as the production acceleration package. This avoids committing to pre-stable approximate-index behavior.

## Ollama embeddings

Recall uses Ollama `/api/embed` with `embeddinggemma`. Every vector records model, dimensions, content hash, status, and timestamp so migrations and stale-vector repair are deterministic.

## JetStream delivery

Agent output ingestion uses a file-backed stream, durable pull consumer, explicit acknowledgement, database event IDs, retry backoff, and a dead-letter subject. Recall acknowledges only after durable capture.
