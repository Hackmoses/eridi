"""M12.1: Google sign-in, with a fake browser flow and token refresh: no network in tests."""

from typing import Any

import pytest

from eridi.connectors import gauth
from eridi.connectors.gauth import (
    SCOPES,
    GoogleAccount,
    GoogleAuth,
    GoogleAuthError,
    GoogleSignInNeededError,
    client_config,
)

ACCOUNT = GoogleAccount("01234567890123456789", "a.okafor@lab.example")


class Fakes:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.flows: list[list[str]] = []
        self.refreshed = 0
        self.refresh_fails = False

    def run_flow(self, config: dict[str, Any], scopes: list[str], show: Any) -> dict[str, Any]:
        assert config["installed"]["client_id"] == "cid.apps.googleusercontent.com"
        self.flows.append(scopes)
        return {"token": "AT1", "refresh_token": "RT", "expiry": self.now + 3600}

    def refresh(self, creds: dict[str, Any]) -> dict[str, Any]:
        if self.refresh_fails:
            raise GoogleSignInNeededError()
        assert creds["refresh_token"] == "RT"  # noqa: S105 - test value
        assert creds["client_secret"] == "not-secret"  # noqa: S105 - test value
        self.refreshed += 1
        return {**creds, "token": f"AT{self.refreshed + 1}", "expiry": self.now + 3600}


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


def make(fakes: Fakes) -> GoogleAuth:
    return GoogleAuth(
        "cid.apps.googleusercontent.com", "not-secret",
        run_flow=fakes.run_flow, refresh=fakes.refresh, clock=lambda: fakes.now,
    )  # fmt: skip


def test_scope_is_drive_readonly() -> None:
    assert SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]


def test_client_needs_configuring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(gauth.ENV_CLIENT_ID, raising=False)
    with pytest.raises(GoogleAuthError, match=r"docs/GOOGLE\.md"):
        client_config()
    monkeypatch.setenv(gauth.ENV_CLIENT_ID, "env-id")
    assert client_config("setting-id")["installed"]["client_id"] == "env-id"


def test_sign_in_stores_tokens_in_the_keychain_only(
    fakes: Fakes, memory_keyring: dict[tuple[str, str], str]
) -> None:
    auth = make(fakes)
    tokens: list[str] = []
    account = auth.sign_in(lambda token: tokens.append(token) or ACCOUNT)
    assert tokens == ["AT1"]
    assert account == ACCOUNT and fakes.flows == [SCOPES]
    assert auth.accounts() == [ACCOUNT]
    stored = "".join(v for (service, _), v in memory_keyring.items() if service == "eridi-google")
    assert "RT" in stored  # the refresh token is in the keychain
    # A fresh GoogleAuth (another Eridi process) finds the account and its token.
    again = make(fakes)
    assert again.accounts() == [ACCOUNT] and again.token(ACCOUNT.id) == "AT1"


def test_token_refreshes_near_expiry(fakes: Fakes) -> None:
    auth = make(fakes)
    auth.sign_in(lambda token: ACCOUNT)
    assert auth.token(ACCOUNT.id) == "AT1" and fakes.refreshed == 0
    fakes.now += 3600 - 60  # within the margin
    assert auth.token(ACCOUNT.id) == "AT2" and fakes.refreshed == 1
    assert auth.token(ACCOUNT.id) == "AT2" and fakes.refreshed == 1  # saved


def test_revoked_sign_in_says_sign_in_again(fakes: Fakes) -> None:
    auth = make(fakes)
    auth.sign_in(lambda token: ACCOUNT)
    fakes.now += 7200
    fakes.refresh_fails = True
    with pytest.raises(GoogleSignInNeededError, match=r"a\.okafor@lab\.example"):
        auth.token(ACCOUNT.id)
    with pytest.raises(GoogleSignInNeededError):
        auth.token("someone-else")


def test_no_refresh_token_is_refused(fakes: Fakes) -> None:
    auth = GoogleAuth(
        "cid.apps.googleusercontent.com", "not-secret",
        run_flow=lambda c, s, show: {"token": "AT", "expiry": 0}, refresh=fakes.refresh,
    )  # fmt: skip
    with pytest.raises(GoogleAuthError, match="stay signed in"):
        auth.sign_in(lambda token: ACCOUNT)
    assert auth.accounts() == []
