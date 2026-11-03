"""M14.3–14.4: release-build overrides (signing, updates) appear only with their secrets."""

from test_engine import load_script

AZURE = {
    "AZURE_CLIENT_ID": "id", "AZURE_CLIENT_SECRET": "secret", "AZURE_TENANT_ID": "tenant",
    "AZURE_SIGNING_ENDPOINT": "https://eus.codesigning.azure.net",
    "AZURE_SIGNING_ACCOUNT": "eridi", "AZURE_SIGNING_PROFILE": "eridi-public",
}  # fmt: skip


def test_nothing_without_secrets() -> None:
    assert load_script("release_config").overrides({}) == {}


def test_windows_signing_needs_every_azure_setting() -> None:
    release = load_script("release_config")
    config = release.overrides(AZURE)
    assert config["bundle"]["windows"]["signCommand"] == (
        "trusted-signing-cli -e https://eus.codesigning.azure.net -a eridi -c eridi-public "
        "-d Eridi %1"
    )
    assert release.overrides({**AZURE, "AZURE_SIGNING_PROFILE": ""}) == {}


def test_updates_turn_on_with_the_public_key() -> None:
    config = load_script("release_config").overrides({"TAURI_UPDATER_PUBKEY": "dW50cnVzdGVk\n"})
    assert config["plugins"]["updater"]["pubkey"] == "dW50cnVzdGVk"
    assert config["bundle"]["createUpdaterArtifacts"] is True
