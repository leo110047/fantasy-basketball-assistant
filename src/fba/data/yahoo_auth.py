import base64
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, RLock
from time import monotonic, sleep
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import keyring
from keyring.errors import KeyringError
from pydantic import AwareDatetime, Field, SecretStr

from fba.contracts.base import DataError, PositiveInt, Record
from fba.contracts.inseason import AuthorizationRequired, InseasonParameters
from fba.data.codec import decode


class Credentials(Record):
    client_id: SecretStr
    client_secret: SecretStr
    access_token: SecretStr | None
    refresh_token: SecretStr | None
    expires_at: AwareDatetime | None
    account_id: str | None = None


class TokenResponse(Record):
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_type: Literal["bearer", "Bearer"]
    expires_in: PositiveInt
    xoauth_yahoo_guid: str | None = Field(default=None, repr=False)


class CredentialVault(Protocol):
    def load(self) -> Credentials | None: ...
    def save(self, credentials: Credentials) -> None: ...


class SystemVault:
    def __init__(self, account: str) -> None:
        self.account = account

    def backend_check(self) -> None:
        backend = keyring.get_keyring()
        module = type(backend).__module__
        if module not in (
            "keyring.backends.macOS",
            "keyring.backends.Windows",
            "keyring.backends.SecretService",
        ):
            raise AuthorizationRequired(
                "credentials: OS credential store is unavailable; plaintext storage is prohibited"
            )

    def load(self) -> Credentials | None:
        self.backend_check()
        value = keyring.get_password("fba-inseason", self.account)
        return (
            decode(Credentials, value.encode(), "OS credential store")
            if value is not None
            else None
        )

    def save(self, credentials: Credentials) -> None:
        self.backend_check()
        # SecretStr deliberately redacts model_dump; serialize only at the vault boundary.
        payload = {
            key: value.get_secret_value()
            if isinstance(value, SecretStr)
            else value.isoformat()
            if isinstance(value, datetime)
            else value
            for key, value in credentials
        }
        keyring.set_password("fba-inseason", self.account, json.dumps(payload))


class HttpResult(Record):
    status: int
    body: bytes


class Transport(Protocol):
    def __call__(
        self, url: str, method: str, headers: dict[str, str], body: bytes | None, timeout: float
    ) -> HttpResult: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        del req, fp, code, msg, headers, newurl
        raise DataError("source: redirects are not permitted for authenticated requests")


def http_request(
    url: str, method: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> HttpResult:
    try:
        with build_opener(NoRedirect()).open(
            Request(url, data=body, headers=headers, method=method), timeout=timeout
        ) as response:
            data = response.read(32_000_001)
            if len(data) > 32_000_000:
                raise DataError("source: response exceeded maximum size")
            return HttpResult(status=response.status, body=data)
    except HTTPError as exc:
        # Never expose a response body, URL, header or request containing credentials.
        return HttpResult(status=exc.code, body=b"")
    except (URLError, TimeoutError, OSError) as exc:
        raise DataError(f"source: network request failed ({type(exc).__name__})") from None


class YahooAuth:
    def __init__(
        self,
        vault: CredentialVault,
        params: InseasonParameters,
        transport: Transport = http_request,
        wait: Callable[[float], None] = sleep,
    ) -> None:
        self.vault, self.params, self.transport, self.wait = vault, params, transport, wait
        self.lock = RLock()
        self.pending_state: str | None = None

    def status(self) -> dict[str, str | None]:
        """Expose only account identity and connection state, never credentials."""
        try:
            credentials = self.vault.load()
        except (AuthorizationRequired, KeyringError):
            return {"account_id": None, "status": "作業系統憑證庫無法使用"}
        return {
            "account_id": credentials.account_id if credentials else None,
            "status": "已儲存授權，可自動更新 access token"
            if credentials and credentials.refresh_token
            else "尚未完成 Yahoo 授權",
        }

    def configure(self, client_id: str, client_secret: str) -> str:
        if not client_id.strip() or not client_secret.strip():
            raise AuthorizationRequired("credentials: Client ID and Client Secret are required")
        with self.lock:
            self.vault.save(
                Credentials(
                    client_id=SecretStr(client_id),
                    client_secret=SecretStr(client_secret),
                    access_token=None,
                    refresh_token=None,
                    expires_at=None,
                )
            )
            self.pending_state = secrets.token_urlsafe(32)
        return "https://api.login.yahoo.com/oauth2/request_auth?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": "oob",
                "response_type": "code",
                "scope": "fspt-r",
                "state": self.pending_state,
            }
        )

    def exchange(self, code: str, now: datetime) -> None:
        if not self.pending_state or not code.strip():
            raise AuthorizationRequired(
                "authorization: initiate the Yahoo connection before submitting a code"
            )
        self.token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": "oob"}, now
        )
        self.pending_state = None

    def token_request(self, values: dict[str, str], now: datetime) -> Credentials:
        with self.lock:
            credentials = self.vault.load()
            if credentials is None:
                raise AuthorizationRequired("credentials: connect Yahoo first")
            basic = base64.b64encode(
                (
                    credentials.client_id.get_secret_value()
                    + ":"
                    + credentials.client_secret.get_secret_value()
                ).encode()
            ).decode()
            result = self.transport(
                "https://api.login.yahoo.com/oauth2/get_token",
                "POST",
                {
                    "Authorization": "Basic " + basic,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                urlencode(values).encode(),
                self.params.request_timeout.value,
            )
            if result.status != 200:
                raise AuthorizationRequired(
                    f"Yahoo authorization HTTP {result.status}: 需要重新連結"
                )
            try:
                response = decode(TokenResponse, result.body, "Yahoo token response")
            except DataError:
                raise AuthorizationRequired("Yahoo authorization: invalid token response") from None
            refresh = response.refresh_token or credentials.refresh_token
            if refresh is None:
                raise AuthorizationRequired("Yahoo authorization: refresh token was not returned")
            updated = credentials.model_copy(
                update={
                    "access_token": response.access_token,
                    "refresh_token": refresh,
                    "expires_at": now + timedelta(seconds=response.expires_in),
                    "account_id": response.xoauth_yahoo_guid or credentials.account_id,
                }
            )
            self.vault.save(updated)
            return updated

    def bearer(self, now: datetime, *, force: bool = False) -> str:
        with self.lock:
            credentials = self.vault.load()
            if credentials is None or credentials.refresh_token is None:
                raise AuthorizationRequired("Yahoo: 尚未連結或需要重新連結")
            if (
                force
                or credentials.access_token is None
                or credentials.expires_at is None
                or now >= credentials.expires_at
            ):
                credentials = self.token_request(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": credentials.refresh_token.get_secret_value(),
                        "redirect_uri": "oob",
                    },
                    now,
                )
            if credentials.access_token is None:
                raise AuthorizationRequired("Yahoo: access token unavailable")
            return credentials.access_token.get_secret_value()


class YahooReader:
    """The only fantasy API operation is GET. OAuth POST lives in YahooAuth."""

    def __init__(self, auth: YahooAuth) -> None:
        self.auth = auth
        self.requests = 0
        self.cancelled = Event()
        self.request_limit: int | None = None
        self.deadline: float | None = None

    def get(self, resource: str) -> bytes:
        if (
            not resource
            or resource.startswith("/")
            or any(c in resource for c in ("?", "#", ":", "..", "\\"))
        ):
            raise DataError("Yahoo resource: invalid path")
        refreshed = False
        attempt = 0
        while attempt <= self.auth.params.retry_count.value:
            if self.cancelled.is_set():
                raise DataError("Yahoo.sync: cancelled during shutdown")
            if self.request_limit is not None and self.requests >= self.request_limit:
                raise DataError("Yahoo.sync: request budget exhausted")
            if self.deadline is not None and monotonic() >= self.deadline:
                raise DataError("Yahoo.sync: synchronization time budget exhausted")
            token = self.auth.bearer(datetime.now(UTC))
            self.requests += 1
            try:
                result = self.auth.transport(
                    "https://fantasysports.yahooapis.com/fantasy/v2/" + resource,
                    "GET",
                    {"Authorization": "Bearer " + token, "Accept": "application/xml"},
                    None,
                    self.auth.params.request_timeout.value,
                )
            except DataError:
                result = HttpResult(status=503, body=b"")
            if result.status == 200:
                return result.body
            if result.status == 401 and not refreshed:
                self.auth.bearer(datetime.now(UTC), force=True)
                refreshed = True
                continue
            if result.status not in (429, 500, 502, 503, 504):
                if result.status in (401, 403):
                    raise AuthorizationRequired(
                        f"Yahoo HTTP {result.status}: 需要重新連結或 API 尚未核准"
                    )
                raise DataError(f"Yahoo HTTP {result.status}: read failed")
            if attempt < self.auth.params.retry_count.value:
                self.auth.wait(self.auth.params.retry_seconds.value * 2**attempt)
            attempt += 1
        raise DataError("Yahoo: rate limit or network retry budget exhausted; 同步失敗")
