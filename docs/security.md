# Security model

Recall 2.1 is designed for a private single-user server.

- Recall, Ollama, NATS client, and NATS monitoring ports bind to loopback on the host.
- Tailscale Serve supplies private HTTPS and tailnet policy enforcement.
- Recall bearer authentication protects every data, administration, and metrics route when a token is configured. Liveness remains public and readiness remains loopback-only.
- Token comparison is constant-time. `recall-admin token rotate` retains the previous token for a maximum 24-hour overlap.
- CORS is disabled unless exact origins are configured.
- Request bodies, captures, result counts, graph depth, and per-client request rates are bounded.
- Error responses do not disclose exception text.
- NATS uses separate publisher and Recall users. Recall acknowledges after its database commit and uses a dead-letter subject for poison events.
- Non-loopback Recall startup without authentication is rejected.

Keep token files mode 0600 on Linux and restricted to Administrators, SYSTEM, and LocalService on Windows. Never commit generated deployment environment files or secret directories.

The supported threat model does not include hostile users sharing the same operating-system account, a compromised host administrator, public internet exposure, or tenant isolation.
