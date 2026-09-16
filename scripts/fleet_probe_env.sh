#!/usr/bin/env bash
# What the Runtime actually runs with: image ENV vs container ENV, and which model
# the provider ends up using. Triggered by a repro that printed
# "Ignoring unknown environment override: CR_SEMANTIC_MODEL".
set -u
echo "=== image ENV (CR_*) ==="
docker inspect xiaojiujiu-runtime:test --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^CR_' | sort
echo
echo "=== container ENV (CR_*), xxj-runtime-fleet ==="
docker inspect xxj-runtime-fleet --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^CR_' | sort
echo
echo "=== effective provider settings as the code sees them ==="
docker exec -i xxj-runtime-fleet python3 - <<'PY'
import sys

sys.path.insert(0, "/app/runtime/src")
from companion_runtime.config import load_config
from companion_runtime.providers import build_provider

cfg = load_config()
print("config.semantic.provider =", cfg.semantic.provider)
print("config.semantic.settle_on_ingest =", cfg.semantic.settle_on_ingest)
print("config.semantic.deep_refresh_idle_hours =", cfg.semantic.deep_refresh_idle_hours)
print("config.semantic.unresolved_backlog_threshold =", cfg.semantic.unresolved_backlog_threshold)
print("config.semantic.deep_refresh_min_interval_seconds =",
      cfg.semantic.deep_refresh_min_interval_seconds)
provider = build_provider(cfg.semantic)
print("provider name =", provider.name)
print("provider model =", getattr(provider, "model", "?"))
print("provider base_url =", getattr(provider, "base_url", "?"))
print("provider max_tokens =", getattr(provider, "max_tokens", "?"))
print("provider deep_timeout_s =", getattr(provider, "deep_timeout_s", "?"))
print("provider temperature =", getattr(provider, "temperature", "?"))
print("provider health =", provider.health())
PY
