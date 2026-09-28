import os
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "dummy")

from telegram_mcp import oauth  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def _oauth_test_client():
    app = Starlette(
        routes=[
            Route(
                "/.well-known/oauth-protected-resource",
                oauth.oauth_protected_resource_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-authorization-server",
                oauth.oauth_authorization_server_metadata,
                methods=["GET"],
            ),
            Route("/register", oauth.oauth_register, methods=["POST"]),
            Route("/authorize", oauth.oauth_authorize_get, methods=["GET"]),
            Route("/token", oauth.oauth_token, methods=["POST"]),
        ]
    )
    return TestClient(app)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_redirect_uri_validation_allows_https_and_loopback_http():
    assert oauth._is_valid_redirect_uri("https://client.example/callback")
    assert oauth._is_valid_redirect_uri("http://127.0.0.1:3000/callback")
    assert oauth._is_valid_redirect_uri("http://localhost:3000/callback")


def test_redirect_uri_validation_rejects_insecure_remote_and_fragments():
    assert not oauth._is_valid_redirect_uri("http://client.example/callback")
    assert not oauth._is_valid_redirect_uri("https://client.example/callback#fragment")
    assert not oauth._is_valid_redirect_uri("com.example.app:/callback")


def test_scope_request_rejects_unknown_scope(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_SCOPES", "mcp:tools")

    scopes, error = oauth._validate_scope_request("mcp:tools admin")

    assert scopes == []
    assert error is not None
    assert "admin" in error


def test_access_token_requires_configured_scope(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_REQUIRED_SCOPES", "mcp:tools")
    token_response = oauth._issue_tokens(
        client_id="client",
        scopes=["mcp:tools"],
        resource="http://localhost:8000/mcp",
    )

    assert oauth._access_token_is_valid(token_response["access_token"])


def test_oauth_authorization_code_flow_with_pkce(monkeypatch):
    oauth.oauth_clients.clear()
    oauth.oauth_authorization_codes.clear()
    oauth.oauth_access_tokens.clear()
    oauth.oauth_refresh_tokens.clear()
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("MCP_OAUTH_AUTH_CODE", raising=False)
    monkeypatch.setenv("MCP_OAUTH_AUTO_APPROVE", "true")
    monkeypatch.setenv("MCP_OAUTH_SCOPES", "mcp:tools")
    monkeypatch.setenv("MCP_OAUTH_REQUIRED_SCOPES", "mcp:tools")

    client = _oauth_test_client()
    redirect_uri = "http://127.0.0.1:3000/callback"
    register_response = client.post(
        "/register",
        json={
            "client_name": "Test Client",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert register_response.status_code == 201
    client_id = register_response.json()["client_id"]

    verifier = "test-verifier-value-that-is-long-enough-for-pkce"
    authorize_response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "resource": "http://testserver/mcp",
            "state": "state-1",
            "scope": "mcp:tools",
        },
        follow_redirects=False,
    )
    assert authorize_response.status_code == 302
    redirect_query = parse_qs(urlparse(authorize_response.headers["location"]).query)
    assert redirect_query["state"] == ["state-1"]
    code = redirect_query["code"][0]

    token_response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": "http://testserver/mcp",
        },
    )
    assert token_response.status_code == 200
    token_payload = token_response.json()
    assert token_payload["token_type"] == "Bearer"
    assert token_payload["scope"] == "mcp:tools"
    assert token_payload["refresh_token"]
    assert oauth._access_token_is_valid(
        token_payload["access_token"],
        "http://testserver/mcp",
    )
