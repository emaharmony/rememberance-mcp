# Ubuntu deployment

Supported reference hosts are Ubuntu 22.04 and 24.04 with at least 8 vCPU, 16 GB RAM, and 100 GB SSD. A CPU-only Ollama host is better with 32 GB RAM.

## Install

1. Install Docker Engine, Compose v2, Tailscale, and OpenSSL.
2. Copy the repository release to the host.
3. Run:

```bash
cd deploy/ubuntu
chmod +x install.sh
./install.sh
tailscale serve --bg http://127.0.0.1:8788
```

The installer generates an API token and separate NATS passwords, starts pinned Recall/Ollama/NATS containers, pulls both models, applies migrations, and bootstraps JetStream.

Use the GPU override when the Docker GPU runtime is installed:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

## Host layout

Runtime state uses stable host bind mounts:

- `/var/lib/recall/data`: Recall databases, backups, and generated state;
- `/var/lib/recall/ollama`: Ollama models;
- `/var/lib/recall/nats`: JetStream files.

The installer creates these paths with service-specific ownership. Deployment
configuration and bootstrap secrets remain under `deploy/ubuntu`; both
`recall.env` and `secrets/api-token` must be mode 0600 and must never be
committed or copied into an unencrypted source backup.

Only loopback ports 8788, 11434, 4222, and 8222 are published. Do not add public firewall allowances. Tailscale Serve is the supported remote entry point.

## Upgrade and rollback

1. Run `docker compose exec recall recall-admin backup`.
2. Save the current image tag and Compose files.
3. Pull/build the new release and run `docker compose up -d`.
4. Run `recall-admin migrate`, then check `/health/ready`.
5. On failure, stop Recall, restore the backup, restore the prior Compose files/image, and start again.

A rollback that follows a schema migration must restore the matching database backup.

## Troubleshooting

```bash
docker compose ps
docker compose logs --tail=200 recall nats ollama
curl --fail http://127.0.0.1:8788/health/live
docker compose exec recall recall-admin doctor
docker compose exec recall recall-admin integrity-check
tailscale serve status
```

If Recall cannot authenticate to NATS, confirm that `RECALL_NATS_URL` and
`NATS_RECALL_PASSWORD` in `recall.env` contain the same generated password,
then rerun `docker compose exec recall recall-admin nats bootstrap`. If a bind
mount is not writable, rerun `install.sh` so it restores the certified
service ownership. Do not loosen the directories to world-writable.

## Removal

Run `docker compose down` to remove containers while preserving
`/var/lib/recall`. Remove the Tailscale Serve rule separately. For complete
removal, first verify an off-host restore, stop publishers, run
`docker compose down`, delete the deployment secret/configuration directory,
and then explicitly remove `/var/lib/recall`. That last deletion is permanent
and removes databases, models, backups, and the JetStream store.
