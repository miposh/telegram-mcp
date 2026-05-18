"""OAuth 2.1 / MCP HTTP authorization layer for the Telegram MCP server.

Provides Dynamic Client Registration, authorization-code + PKCE flow,
refresh tokens, and a Starlette ASGI middleware that enforces bearer-token
access on the MCP streamable-HTTP endpoint.

Configured via environment variables (all optional unless noted):
  MCP_OAUTH_ENABLED          enable OAuth protection (default: true)
  MCP_OAUTH_AUTH_CODE        shared approval code (required unless auto-approve)
  MCP_OAUTH_AUTO_APPROVE     skip the approval prompt (default: false)
  MCP_OAUTH_SCOPES           supported scopes (default: "mcp:tools")
  MCP_OAUTH_REQUIRED_SCOPES  scopes required on the token (default: supported)
  MCP_OAUTH_ISSUER           override issuer URL (default: request origin)
  MCP_OAUTH_RESOURCE         override resource URL (default: origin + MCP_PATH)
  MCP_OAUTH_RESOURCE_DOCUMENTATION  resource_documentation field
  MCP_OAUTH_ACCESS_TOKEN_TTL access-token TTL in seconds (default: 3600)
  MCP_OAUTH_AUTH_CODE_TTL    auth-code TTL in seconds (default: 300)
  MCP_OAUTH_REFRESH_TOKEN_TTL refresh-token TTL in seconds (default: 30 days)
  MCP_PATH                   streamable-HTTP mount path (default: /mcp)
  MCP_PUBLIC_URL             public origin override for issuer/resource URLs
  MCP_HOST / MCP_PORT        uvicorn bind host/port (default: 0.0.0.0:8000)
  MCP_UVICORN_LOG_LEVEL      uvicorn log level (default: info)
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import secrets
import time
from html import escape
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from telegram_mcp.runtime import mcp


OAUTH_DEFAULT_SCOPES = ["mcp:tools"]
OAUTH_ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("MCP_OAUTH_ACCESS_TOKEN_TTL", "3600"))
OAUTH_AUTH_CODE_TTL_SECONDS = int(os.getenv("MCP_OAUTH_AUTH_CODE_TTL", "300"))
OAUTH_REFRESH_TOKEN_TTL_SECONDS = int(
    os.getenv("MCP_OAUTH_REFRESH_TOKEN_TTL", str(30 * 24 * 60 * 60))
)

oauth_clients: dict[str, dict[str, Any]] = {}
oauth_authorization_codes: dict[str, dict[str, Any]] = {}
oauth_access_tokens: dict[str, dict[str, Any]] = {}
oauth_refresh_tokens: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def oauth_http_enabled() -> bool:
    return _env_bool("MCP_OAUTH_ENABLED", True)


def normalize_mcp_path() -> str:
    configured_path = os.getenv("MCP_PATH", "/mcp").strip() or "/mcp"
    if not configured_path.startswith("/"):
        configured_path = f"/{configured_path}"
    return configured_path.rstrip("/") or "/"


def _oauth_supported_scopes() -> list[str]:
    raw_scopes = os.getenv("MCP_OAUTH_SCOPES")
    if not raw_scopes:
        return OAUTH_DEFAULT_SCOPES.copy()
    scopes = [scope for scope in re.split(r"[\s,]+", raw_scopes.strip()) if scope]
    return scopes or OAUTH_DEFAULT_SCOPES.copy()


def _oauth_required_scopes() -> list[str]:
    raw_scopes = os.getenv("MCP_OAUTH_REQUIRED_SCOPES")
    if not raw_scopes:
        return _oauth_supported_scopes()
    scopes = [scope for scope in re.split(r"[\s,]+", raw_scopes.strip()) if scope]
    return scopes or _oauth_supported_scopes()


def _scope_string(scopes: list[str]) -> str:
    return " ".join(scopes)


def _request_origin(request: Any) -> str:
    public_url = os.getenv("MCP_PUBLIC_URL")
    if public_url:
        return public_url.rstrip("/")

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    proto = forwarded_proto.split(",")[0].strip() if forwarded_proto else request.url.scheme
    host = forwarded_host.split(",")[0].strip() if forwarded_host else request.headers["host"]
    return f"{proto}://{host}".rstrip("/")


def _oauth_issuer_url(request: Any) -> str:
    return os.getenv("MCP_OAUTH_ISSUER", _request_origin(request)).rstrip("/")


def _oauth_resource_url(request: Any) -> str:
    return os.getenv(
        "MCP_OAUTH_RESOURCE",
        f"{_request_origin(request)}{normalize_mcp_path()}",
    ).rstrip("/")


def _is_loopback_host(hostname: Optional[str]) -> bool:
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _is_valid_redirect_uri(raw_uri: str) -> bool:
    parsed = urlparse(raw_uri)
    if not parsed.scheme or not parsed.netloc or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http" and _is_loopback_host(parsed.hostname):
        return True
    return False


def _validate_redirect_uris(redirect_uris: Any) -> tuple[list[str], Optional[str]]:
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return [], "redirect_uris must be a non-empty list"

    clean_uris: list[str] = []
    for raw_uri in redirect_uris:
        if not isinstance(raw_uri, str) or not _is_valid_redirect_uri(raw_uri):
            return [], (
                "redirect_uris may only contain HTTPS URLs or HTTP loopback URLs "
                "without fragments"
            )
        clean_uris.append(raw_uri)

    return clean_uris, None


def _validate_scope_request(raw_scope: Optional[str]) -> tuple[list[str], Optional[str]]:
    supported_scopes = set(_oauth_supported_scopes())
    if not raw_scope:
        return _oauth_required_scopes(), None

    requested_scopes = [scope for scope in raw_scope.split() if scope]
    unsupported_scopes = sorted(set(requested_scopes) - supported_scopes)
    if unsupported_scopes:
        return [], f"unsupported scopes: {' '.join(unsupported_scopes)}"
    return requested_scopes, None


def _client_metadata_response(client: dict[str, Any]) -> dict[str, Any]:
    response = {
        "client_id": client["client_id"],
        "client_id_issued_at": client["client_id_issued_at"],
        "client_name": client.get("client_name", "MCP Client"),
        "redirect_uris": client["redirect_uris"],
        "grant_types": client["grant_types"],
        "response_types": client["response_types"],
        "token_endpoint_auth_method": "none",
    }
    if client.get("scope"):
        response["scope"] = client["scope"]
    return response


def _build_www_authenticate(request: Any, error: Optional[str] = None) -> str:
    params = [
        'realm="telegram-mcp"',
        f'resource_metadata="{_request_origin(request)}/.well-known/oauth-protected-resource"',
        f'scope="{_scope_string(_oauth_required_scopes())}"',
    ]
    if error:
        params.append(f'error="{error}"')
    return "Bearer " + ", ".join(params)


def _cleanup_oauth_state() -> None:
    now = time.time()
    for store in (oauth_authorization_codes, oauth_access_tokens, oauth_refresh_tokens):
        expired_keys = [
            key for key, value in store.items() if float(value.get("expires_at", 0)) <= now
        ]
        for key in expired_keys:
            store.pop(key, None)


def _code_challenge_matches(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    calculated = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(calculated, challenge)


def _issue_tokens(
    client_id: str,
    scopes: list[str],
    resource: str,
    include_refresh_token: bool = True,
) -> dict[str, Any]:
    now = int(time.time())
    access_token = secrets.token_urlsafe(32)

    oauth_access_tokens[access_token] = {
        "client_id": client_id,
        "scope": scopes,
        "resource": resource,
        "issued_at": now,
        "expires_at": now + OAUTH_ACCESS_TOKEN_TTL_SECONDS,
    }

    token_response = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": OAUTH_ACCESS_TOKEN_TTL_SECONDS,
        "scope": _scope_string(scopes),
    }

    if include_refresh_token:
        refresh_token = secrets.token_urlsafe(32)
        oauth_refresh_tokens[refresh_token] = {
            "client_id": client_id,
            "scope": scopes,
            "resource": resource,
            "issued_at": now,
            "expires_at": now + OAUTH_REFRESH_TOKEN_TTL_SECONDS,
        }
        token_response["refresh_token"] = refresh_token

    return token_response


def _access_token_is_valid(token: str, resource: Optional[str] = None) -> bool:
    _cleanup_oauth_state()
    token_data = oauth_access_tokens.get(token)
    if not token_data:
        return False
    if resource and token_data.get("resource") != resource.rstrip("/"):
        return False
    granted_scopes = set(token_data.get("scope", []))
    return set(_oauth_required_scopes()).issubset(granted_scopes)


def _oauth_error_response(error: str, description: str, status_code: int = 400) -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _request_form_data(request: Any) -> dict[str, str]:
    raw_body = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw_body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


async def oauth_protected_resource_metadata(request: Any) -> Any:
    from starlette.responses import JSONResponse

    resource_url = _oauth_resource_url(request)
    issuer_url = _oauth_issuer_url(request)
    return JSONResponse(
        {
            "resource": resource_url,
            "authorization_servers": [issuer_url],
            "scopes_supported": _oauth_supported_scopes(),
            "bearer_methods_supported": ["header"],
            "resource_documentation": os.getenv(
                "MCP_OAUTH_RESOURCE_DOCUMENTATION",
                f"{_request_origin(request)}/",
            ),
        }
    )


async def oauth_authorization_server_metadata(request: Any) -> Any:
    from starlette.responses import JSONResponse

    issuer_url = _oauth_issuer_url(request)
    return JSONResponse(
        {
            "issuer": issuer_url,
            "authorization_endpoint": f"{issuer_url}/authorize",
            "token_endpoint": f"{issuer_url}/token",
            "registration_endpoint": f"{issuer_url}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": _oauth_supported_scopes(),
            "resource_indicators_supported": True,
            "client_id_metadata_document_supported": False,
        }
    )


async def oauth_register(request: Any) -> Any:
    from starlette.responses import JSONResponse

    try:
        payload = await request.json()
    except Exception:
        return _oauth_error_response("invalid_client_metadata", "Request body must be JSON")

    redirect_uris, redirect_error = _validate_redirect_uris(payload.get("redirect_uris"))
    if redirect_error:
        return _oauth_error_response("invalid_client_metadata", redirect_error)

    grant_types = payload.get("grant_types") or ["authorization_code", "refresh_token"]
    response_types = payload.get("response_types") or ["code"]
    token_auth_method = payload.get("token_endpoint_auth_method", "none")

    if (
        not isinstance(grant_types, list)
        or "authorization_code" not in grant_types
        or not set(grant_types).issubset({"authorization_code", "refresh_token"})
    ):
        return _oauth_error_response("invalid_client_metadata", "Unsupported grant_types")
    if not isinstance(response_types, list) or response_types != ["code"]:
        return _oauth_error_response("invalid_client_metadata", "response_types must be ['code']")
    if token_auth_method != "none":
        return _oauth_error_response(
            "invalid_client_metadata",
            "Only public clients with token_endpoint_auth_method 'none' are supported",
        )

    client_id = f"mcp_{secrets.token_urlsafe(24)}"
    requested_scopes, scope_error = _validate_scope_request(payload.get("scope"))
    if scope_error:
        return _oauth_error_response("invalid_client_metadata", scope_error)

    oauth_clients[client_id] = {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": payload.get("client_name") or "MCP Client",
        "redirect_uris": redirect_uris,
        "grant_types": grant_types,
        "response_types": response_types,
        "scope": _scope_string(requested_scopes),
    }

    return JSONResponse(_client_metadata_response(oauth_clients[client_id]), status_code=201)


def _authorization_error_redirect(redirect_uri: str, error: str, state: Optional[str]) -> Any:
    from starlette.responses import RedirectResponse

    query_params = {"error": error}
    if state:
        query_params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(query_params)}", status_code=302)


def _render_authorization_form(params: dict[str, str], client: dict[str, Any]) -> Any:
    from starlette.responses import HTMLResponse

    hidden_inputs = "\n".join(
        f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
        for key, value in params.items()
    )
    approval_field = ""
    if not _env_bool("MCP_OAUTH_AUTO_APPROVE", False):
        approval_field = """
            <label>
                Approval code
                <input name="approval_code" type="password" autocomplete="one-time-code" required>
            </label>
        """

    html = f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Authorize Telegram MCP</title>
        <style>
            body {{
                font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                margin: 0;
                min-height: 100vh;
                display: grid;
                place-items: center;
                background: #f6f8fa;
                color: #1f2328;
            }}
            main {{
                width: min(420px, calc(100vw - 32px));
                background: white;
                border: 1px solid #d0d7de;
                border-radius: 8px;
                padding: 24px;
                box-shadow: 0 8px 24px rgba(140, 149, 159, .2);
            }}
            h1 {{ font-size: 20px; margin: 0 0 12px; }}
            p {{ line-height: 1.45; color: #57606a; }}
            label {{ display: grid; gap: 8px; font-weight: 600; margin: 18px 0; }}
            input {{
                font: inherit;
                padding: 10px 12px;
                border: 1px solid #d0d7de;
                border-radius: 6px;
            }}
            button {{
                width: 100%;
                border: 0;
                border-radius: 6px;
                padding: 11px 14px;
                font: inherit;
                font-weight: 600;
                color: white;
                background: #0969da;
                cursor: pointer;
            }}
        </style>
    </head>
    <body>
        <main>
            <h1>Authorize Telegram MCP</h1>
            <p>
                <strong>{escape(client.get("client_name", "MCP Client"))}</strong>
                is requesting access to this Telegram MCP server.
            </p>
            <p>Scopes: {escape(params.get("scope", ""))}</p>
            <form method="post" action="/authorize">
                {hidden_inputs}
                {approval_field}
                <button type="submit">Authorize</button>
            </form>
        </main>
    </body>
    </html>
    """
    return HTMLResponse(html)


def _validate_authorization_params(
    params: dict[str, str], request: Any
) -> tuple[Optional[dict[str, Any]], Optional[list[str]], Optional[Any]]:
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    response_type = params.get("response_type", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")
    resource = params.get("resource", "").rstrip("/")
    state = params.get("state")

    client = oauth_clients.get(client_id)
    if not client:
        return None, None, _oauth_error_response("invalid_client", "Unknown client_id")
    if redirect_uri not in client["redirect_uris"]:
        return None, None, _oauth_error_response(
            "invalid_request",
            "redirect_uri must exactly match a registered redirect URI",
        )
    if response_type != "code":
        return client, None, _authorization_error_redirect(
            redirect_uri,
            "unsupported_response_type",
            state,
        )
    if not code_challenge or code_challenge_method != "S256":
        return client, None, _authorization_error_redirect(redirect_uri, "invalid_request", state)
    if resource != _oauth_resource_url(request):
        return client, None, _authorization_error_redirect(redirect_uri, "invalid_target", state)

    scopes, scope_error = _validate_scope_request(params.get("scope"))
    if scope_error:
        return client, None, _authorization_error_redirect(redirect_uri, "invalid_scope", state)
    if not set(_oauth_required_scopes()).issubset(set(scopes)):
        return client, None, _authorization_error_redirect(redirect_uri, "invalid_scope", state)

    return client, scopes, None


async def oauth_authorize_get(request: Any) -> Any:
    params = dict(request.query_params)
    client, scopes, error_response = _validate_authorization_params(params, request)
    if error_response:
        return error_response
    assert client is not None
    assert scopes is not None
    params["scope"] = _scope_string(scopes)

    if _env_bool("MCP_OAUTH_AUTO_APPROVE", False):
        return _complete_authorization(params, scopes)

    if not os.getenv("MCP_OAUTH_AUTH_CODE"):
        return _oauth_error_response(
            "server_error",
            "MCP_OAUTH_AUTH_CODE must be configured or MCP_OAUTH_AUTO_APPROVE=true must be set",
            status_code=503,
        )

    return _render_authorization_form(params, client)


async def oauth_authorize_post(request: Any) -> Any:
    params = await _request_form_data(request)
    client, scopes, error_response = _validate_authorization_params(params, request)
    if error_response:
        return error_response
    assert scopes is not None

    expected_code = os.getenv("MCP_OAUTH_AUTH_CODE")
    provided_code = params.get("approval_code", "")
    if not _env_bool("MCP_OAUTH_AUTO_APPROVE", False):
        if not expected_code:
            return _oauth_error_response(
                "server_error",
                "MCP_OAUTH_AUTH_CODE must be configured",
                status_code=503,
            )
        if not secrets.compare_digest(provided_code, expected_code):
            return _oauth_error_response("access_denied", "Invalid approval code", status_code=403)

    return _complete_authorization(params, scopes)


def _complete_authorization(params: dict[str, str], scopes: list[str]) -> Any:
    from starlette.responses import RedirectResponse

    code = secrets.token_urlsafe(32)
    now = int(time.time())
    oauth_authorization_codes[code] = {
        "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "scope": scopes,
        "resource": params["resource"].rstrip("/"),
        "code_challenge": params["code_challenge"],
        "expires_at": now + OAUTH_AUTH_CODE_TTL_SECONDS,
    }

    query_params = {"code": code}
    if params.get("state"):
        query_params["state"] = params["state"]
    separator = "&" if "?" in params["redirect_uri"] else "?"
    return RedirectResponse(
        f"{params['redirect_uri']}{separator}{urlencode(query_params)}",
        status_code=302,
    )


async def oauth_token(request: Any) -> Any:
    from starlette.responses import JSONResponse

    form = await _request_form_data(request)
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code", "")
        code_data = oauth_authorization_codes.pop(code, None)
        if not code_data or float(code_data.get("expires_at", 0)) <= time.time():
            return _oauth_error_response("invalid_grant", "Invalid or expired authorization code")
        if form.get("client_id") != code_data["client_id"]:
            return _oauth_error_response(
                "invalid_grant",
                "client_id does not match authorization code",
            )
        if form.get("redirect_uri") != code_data["redirect_uri"]:
            return _oauth_error_response(
                "invalid_grant",
                "redirect_uri does not match authorization code",
            )
        if form.get("resource", "").rstrip("/") != code_data["resource"]:
            return _oauth_error_response(
                "invalid_target",
                "resource does not match authorization code",
            )
        if not _code_challenge_matches(form.get("code_verifier", ""), code_data["code_challenge"]):
            return _oauth_error_response("invalid_grant", "PKCE verification failed")

        token_response = _issue_tokens(
            code_data["client_id"],
            code_data["scope"],
            code_data["resource"],
            include_refresh_token=(
                "refresh_token"
                in oauth_clients.get(code_data["client_id"], {}).get("grant_types", [])
            ),
        )
        return JSONResponse(
            token_response,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    if grant_type == "refresh_token":
        refresh_token = form.get("refresh_token", "")
        refresh_data = oauth_refresh_tokens.pop(refresh_token, None)
        if not refresh_data or float(refresh_data.get("expires_at", 0)) <= time.time():
            return _oauth_error_response("invalid_grant", "Invalid or expired refresh token")
        if form.get("client_id") != refresh_data["client_id"]:
            return _oauth_error_response("invalid_grant", "client_id does not match refresh token")
        resource = form.get("resource", refresh_data["resource"]).rstrip("/")
        if resource != refresh_data["resource"]:
            return _oauth_error_response("invalid_target", "resource does not match refresh token")

        token_response = _issue_tokens(
            refresh_data["client_id"],
            refresh_data["scope"],
            refresh_data["resource"],
            include_refresh_token=True,
        )
        return JSONResponse(
            token_response,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    return _oauth_error_response("unsupported_grant_type", "Unsupported grant_type")


class OAuthProtectedASGI:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.mcp_path = normalize_mcp_path()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not oauth_http_enabled():
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_mcp_request = path == self.mcp_path or path.startswith(f"{self.mcp_path}/")
        if not is_mcp_request or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope, receive=receive)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")

        if scheme.lower() != "bearer" or not token:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": _build_www_authenticate(request)},
            )
            await response(scope, receive, send)
            return

        if not _access_token_is_valid(token, _oauth_resource_url(request)):
            response = JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": _build_www_authenticate(request, "invalid_token")},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def run_oauth_http_server() -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    @contextlib.asynccontextmanager
    async def lifespan(app: Any):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route(
                "/.well-known/oauth-protected-resource",
                oauth_protected_resource_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-authorization-server",
                oauth_authorization_server_metadata,
                methods=["GET"],
            ),
            Route("/register", oauth_register, methods=["POST"]),
            Route("/authorize", oauth_authorize_get, methods=["GET"]),
            Route("/authorize", oauth_authorize_post, methods=["POST"]),
            Route("/token", oauth_token, methods=["POST"]),
            Mount("/", app=OAuthProtectedASGI(mcp.streamable_http_app())),
        ],
        lifespan=lifespan,
    )

    config = uvicorn.Config(
        app,
        host=os.getenv("MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("MCP_PORT", "8000")),
        log_level=os.getenv("MCP_UVICORN_LOG_LEVEL", "info").lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()
