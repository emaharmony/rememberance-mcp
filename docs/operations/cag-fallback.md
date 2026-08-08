# CAG fallback

`fallback_full` is the expected safe response when claimed client state, a
fingerprint, a dependency, a cached artifact, or delta generation cannot be
verified. Recall rebuilds a complete Context Pack V2 from canonical sources,
includes a sanitized reason, records the fallback, and reports zero artificial
savings. A partial delta is never labeled complete.

Disable optimization with `RECALL_CAG_ENABLED=false`; full context delivery
continues to work. Cache write and secondary telemetry failures do not mutate
canonical data.
