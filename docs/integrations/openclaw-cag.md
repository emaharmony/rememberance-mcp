# OpenClaw CAG integration

OpenClaw may request structured JSON or Markdown while using the same known-state
contract. Store only disposable authoritative IDs and versions, discard them on
scope change, honor refresh/removal instructions, and expand evidence on demand.
No OpenClaw-specific canonical cache or repository adapter is included yet.
