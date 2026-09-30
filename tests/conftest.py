from __future__ import annotations

import os

# config.py raises EnvironmentError at import time if OPENAI_API_KEY isn't set — set a dummy
# value before any test imports config/realtime_client, so tests never depend on a real .env.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key")
# Avoid touching the real OS keyring during unit tests that import assistant/factory.
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "credentials/client_secret.json")
