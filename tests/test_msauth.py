"""M10.1: Microsoft sign-in (MSAL), with a fake MSAL app: no network in tests."""

from typing import Any

import pytest

from eridi.connectors import msauth
from eridi.connectors.msauth import (
    SCOPES,
    ApprovalRequiredError,
    KeyringTokenStore,
    MicrosoftAuth,
    MicrosoftAuthError,
    SignInNeededError,
    client_id,
)

CLIENT = "11111111-2222-3333-4444-555555555555"
ACCOUNT = {"home_account_id": "uid.tid", "username": "a.okafor@uni.example"}
CLAIMS = {"preferred_username": "a.okafor@uni.example", "tid": "tid"}


class FakeApp:
    """Stands in for msal.PublicClientApplication."""

    def __init__(self, client: str, cache: Any) -> None:
        self.client = client
        self.cache = cache
        self.accounts: list[dict[str, str]] = []
        self.result: dict[str, Any] = {"access_token": "AT1", "id_token_claims": CLAIMS}
        self.silent: dict[str, Any] | None = {"access_token": "AT2"}
        self.calls: list[tuple[str, Any]] = []

    def _signed_in(self) -> dict[str, Any]:
        if "access_token" in self.result:
            self.accounts = [ACCOUNT]
            self.cache.has_state_changed = True  # as MSAL does after a sign-in
        return self.result

    def acquire_token_interactive(self, scopes: list[str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("interactive", (scopes, kwargs)))
        return self._signed_in()

    def initiate_device_flow(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append(("device", scopes))
        return {
            "user_code": "ABCD",
            "message": "Go to https://microsoft.com/devicelogin, enter ABCD",
        }

    def acquire_token_by_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        return self._signed_in()

    def get_accounts(self) -> list[dict[str, str]]:
        return self.accounts

    def acquire_token_silent(self, scopes: list[str], account: Any) -> dict[str, Any] | None:
        self.calls.append(("silent", account))
        return self.silent


@pytest.fixture
def app() -> FakeApp:
    return FakeApp(CLIENT, None)


def auth_with(app: FakeApp) -> MicrosoftAuth:
    def factory(client: str, cache: Any) -> FakeApp:
        app.client, app.cache = client, cache
        return app

    return MicrosoftAuth(CLIENT, app_factory=factory)


def test_scopes_are_read_only() -> None:
    assert SCOPES == ["Files.Read.All", "Sites.Read.All", "User.Read"]
    assert not any("Write" in s for s in SCOPES)


def test_client_id_needs_configuring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(msauth.ENV_CLIENT_ID, raising=False)
    monkeypatch.setattr(msauth, "DEFAULT_CLIENT_ID", "")
    with pytest.raises(MicrosoftAuthError, match="no Microsoft app ID"):
        client_id()
    assert client_id("from-settings") == "from-settings"
    monkeypatch.setenv(msauth.ENV_CLIENT_ID, "from-env")
    assert client_id("from-settings") == "from-env"


def test_browser_sign_in_and_silent_tokens(app: FakeApp, memory_keyring: dict[Any, str]) -> None:
    auth = auth_with(app)
    account = auth.sign_in()
    assert account.id == "uid.tid" and account.username == "a.okafor@uni.example"
    kind, (scopes, kwargs) = app.calls[0]
    assert kind == "interactive" and scopes == SCOPES and kwargs["prompt"] == "select_account"
    assert auth.token("uid.tid") == "AT2"
    # The cache went to the keychain, never to a file.
    assert any(k[0] == msauth.KEYRING_SERVICE for k in memory_keyring)


def test_device_code_sign_in(app: FakeApp) -> None:
    shown: list[str] = []
    account = auth_with(app).sign_in(device_code=True, show=shown.append)
    assert account.username == "a.okafor@uni.example"
    assert "devicelogin" in shown[0] and "ABCD" in shown[0]


def test_no_browser_suggests_the_device_code(app: FakeApp) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("could not open browser")

    app.acquire_token_interactive = broken  # type: ignore[method-assign]
    with pytest.raises(MicrosoftAuthError, match="--device-code"):
        auth_with(app).sign_in()


@pytest.mark.parametrize(
    "error",
    [
        {"error": "invalid_grant", "error_description": "AADSTS65001: The user or "
         "administrator has not consented to use the application"},
        {"error": "invalid_request", "error_description": "AADSTS90094: Admin consent is "
         "required for the permissions requested by this application."},
        {"error": "consent_required", "error_description": "consent needed"},
    ],
)  # fmt: skip
def test_it_approval_is_explained(app: FakeApp, error: dict[str, Any]) -> None:
    app.result = {**error, "id_token_claims": {"tid": "uni-tenant"}}
    with pytest.raises(ApprovalRequiredError) as info:
        auth_with(app).sign_in()
    message = str(info.value)
    assert message.startswith("Your university needs to approve this app. Send IT this link:")
    assert info.value.consent_url == (
        f"https://login.microsoftonline.com/uni-tenant/adminconsent?client_id={CLIENT}"
    )
    assert "docs/IT_APPROVAL.md" in message


def test_other_failures(app: FakeApp) -> None:
    app.result = {"error": "access_denied", "error_description": "The user cancelled."}
    with pytest.raises(MicrosoftAuthError, match="cancelled"):
        auth_with(app).sign_in()
    app.result = {"error": "server_error", "error_description": "AADSTS500011: boom\nTrace ID"}
    with pytest.raises(MicrosoftAuthError, match=r"AADSTS500011: boom$"):
        auth_with(app).sign_in()


def test_token_needs_sign_in(app: FakeApp) -> None:
    auth = auth_with(app)
    with pytest.raises(SignInNeededError, match="add-microsoft"):
        auth.token("uid.tid")
    auth.sign_in()
    app.silent = None  # the refresh token expired or was revoked
    with pytest.raises(SignInNeededError, match=r"a\.okafor@uni\.example"):
        auth.token("uid.tid")


def test_keyring_store_splits_long_caches(memory_keyring: dict[Any, str]) -> None:
    store = KeyringTokenStore(CLIENT)
    assert store.load() is None
    long = "x" * (msauth.CHUNK * 3 + 7)
    store.save(long)
    assert store.load() == long
    assert all(len(v) <= msauth.CHUNK for v in memory_keyring.values())
    store.save("short")
    assert store.load() == "short"
    assert memory_keyring[(msauth.KEYRING_SERVICE, f"cache:{CLIENT}:3")] == ""  # blanked
    memory_keyring.pop((msauth.KEYRING_SERVICE, f"cache:{CLIENT}:0"))
    assert store.load() is None  # damaged: treated as signed out


def test_no_keychain_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring
    import keyring.errors

    def missing(*args: Any) -> None:
        raise keyring.errors.NoKeyringError("No recommended backend was available.")

    monkeypatch.setattr(keyring, "get_password", missing)
    with pytest.raises(MicrosoftAuthError, match="keychain"):
        MicrosoftAuth(CLIENT, app_factory=lambda c, cache: FakeApp(c, cache))


def test_offline_token_refresh_is_explained(app: FakeApp) -> None:
    auth = auth_with(app)
    auth.sign_in()

    def offline(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("Name or service not known")

    app.acquire_token_silent = offline  # type: ignore[method-assign]
    with pytest.raises(MicrosoftAuthError, match="internet connection"):
        auth.token("uid.tid")
