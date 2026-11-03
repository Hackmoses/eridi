"""Tauri config overrides for a release build, from the environment (CI secrets).

    uv run python scripts/release_config.py > desktop/release.conf.json
    npm run tauri build -- --config release.conf.json

Each part is included only when its secrets are set, so a fork or a test run without them
still builds (unsigned, without updates):
- Windows signing with Azure Trusted Signing: AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
  AZURE_TENANT_ID (read by the signing tool) and AZURE_SIGNING_ENDPOINT,
  AZURE_SIGNING_ACCOUNT, AZURE_SIGNING_PROFILE (which certificate profile).
- Updates: TAURI_UPDATER_PUBKEY (the public half of `tauri signer generate`); the build
  then also needs TAURI_SIGNING_PRIVATE_KEY (and its password) to sign update bundles.
macOS signing and notarization need no override: Tauri reads APPLE_CERTIFICATE,
APPLE_CERTIFICATE_PASSWORD, APPLE_SIGNING_IDENTITY, APPLE_ID, APPLE_PASSWORD and
APPLE_TEAM_ID itself.
"""

import json
import os
from collections.abc import Mapping
from typing import Any

AZURE = ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_SIGNING_ENDPOINT",
         "AZURE_SIGNING_ACCOUNT", "AZURE_SIGNING_PROFILE")  # fmt: skip


def windows_sign_command(env: Mapping[str, str]) -> str | None:
    """The command Tauri runs on each Windows executable and installer (`%1` = the file)."""
    if not all(env.get(k) for k in AZURE):
        return None
    return (
        f"trusted-signing-cli -e {env['AZURE_SIGNING_ENDPOINT']} -a {env['AZURE_SIGNING_ACCOUNT']}"
        f" -c {env['AZURE_SIGNING_PROFILE']} -d Eridi %1"
    )


def overrides(env: Mapping[str, str]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    sign = windows_sign_command(env)
    if sign:
        config.setdefault("bundle", {})["windows"] = {"signCommand": sign}
    pubkey = env.get("TAURI_UPDATER_PUBKEY", "").strip()
    if pubkey:
        config.setdefault("bundle", {})["createUpdaterArtifacts"] = True
        config["plugins"] = {"updater": {"pubkey": pubkey}}
    return config


if __name__ == "__main__":
    print(json.dumps(overrides(os.environ), indent=2))
