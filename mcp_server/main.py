import os
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx
from jsonschema import validate, ValidationError
from fastmcp import FastMCP

mcp = FastMCP("API Testing MCP")

# Only approved public API hosts
ALLOWED_HOSTS = {
    "jsonplaceholder.typicode.com",
    "postman-echo.com",
}

# Basic per-process rate limit
REQUEST_LIMIT = 20
WINDOW_SECONDS = 60
CALLS = defaultdict(deque)


def check_rate_limit():
    now = time.monotonic()
    calls = CALLS["global"]

    while calls and now - calls[0] >= WINDOW_SECONDS:
        calls.popleft()

    if len(calls) >= REQUEST_LIMIT:
        raise ValueError("Rate limit exceeded. Try again later.")

    calls.append(now)


def validate_url(url: str):
    parsed = urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError(
            "Use HTTPS with an approved API hostname."
        )


async def make_request(url: str, method="GET", body=None):
    validate_url(url)

    # Do not follow redirects to other destinations.
    async with httpx.AsyncClient(
        timeout=8.0,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.request(
            method,
            url,
            json=body,
        )

    # Limit response size before returning data.
    raw = response.content[:20000]
    text = raw.decode("utf-8", errors="replace")

    try:
        data = response.json()
        is_json = True
    except ValueError:
        data = text
        is_json = False

    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "is_json": is_json,
        "body": data,
    }


@mcp.tool()
async def test_api(
    url: str,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    """Test an approved public API and return its response."""
    check_rate_limit()

    method = method.upper()
    if method not in {"GET", "POST"}:
        raise ValueError("Only GET and POST are supported.")

    return await make_request(url, method, body)


@mcp.tool()
async def validate_json(
    url: str,
    schema: dict,
) -> dict:
    """Validate a public API's JSON response against a schema."""
    check_rate_limit()

    result = await make_request(url)

    if not 200 <= result["status"] < 300:
        return {
            "valid": False,
            "error": f"HTTP status {result['status']}",
        }

    if not result["is_json"]:
        return {
            "valid": False,
            "error": "Response is not valid JSON",
        }

    try:
        validate(instance=result["body"], schema=schema)
        return {"valid": True, "message": "Schema validation passed"}
    except ValidationError as exc:
        return {
            "valid": False,
            "error": exc.message,
            "path": list(exc.absolute_path),
        }


@mcp.tool()
async def compare_apis(url_a: str, url_b: str) -> dict:
    """Compare status codes and JSON bodies of two APIs."""
    check_rate_limit()

    result_a = await make_request(url_a)
    result_b = await make_request(url_b)

    return {
        "status_match": result_a["status"] == result_b["status"],
        "json_match": (
            result_a["is_json"]
            and result_b["is_json"]
            and result_a["body"] == result_b["body"]
        ),
        "api_a": result_a,
        "api_b": result_b,
    }


@mcp.tool()
async def generate_report(url: str) -> dict:
    """Generate a concise API test report."""
    check_rate_limit()

    result = await make_request(url)
    passed = (
        200 <= result["status"] < 300
        and result["is_json"]
    )

    return {
        "url": url,
        "status": result["status"],
        "is_json": result["is_json"],
        "passed": passed,
        "summary": (
            "API test passed"
            if passed
            else "API response needs investigation"
        ),
    }


if __name__ == "__main__":
    print("Starting MCP server...", flush=True)

    mcp.run(
        transport="http",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
    )