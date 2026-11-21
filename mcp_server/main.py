import os
import time
import copy
import json
import socket
import ipaddress
import asyncio

from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx
from jsonschema import validate, ValidationError
from fastmcp import FastMCP

mcp = FastMCP("API Testing MCP")


# Rate limiting
REQUEST_LIMIT = 20
WINDOW_SECONDS = 60
CALLS = defaultdict(deque)

# Maximum response size: 20 KB
MAX_RESPONSE_SIZE = 20_000


def check_rate_limit():
    now = time.monotonic()
    calls = CALLS["global"]

    while calls and now - calls[0] >= WINDOW_SECONDS:
        calls.popleft()

    if len(calls) >= REQUEST_LIMIT:
        raise ValueError("Rate limit exceeded. Try again later.")

    calls.append(now)


async def validate_url(url: str):
    """
    Allow publicly hosted HTTPS APIs.

    Reject localhost, private, loopback and
    link-local destinations.
    """

    try:
        parsed = urlparse(url)

        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Only public HTTPS URLs on port 443 are allowed.")

        hostname = parsed.hostname.rstrip(".")

        if hostname.lower() == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("Localhost URLs are not allowed.")

        # Resolve hostname to IP addresses.
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            raise ValueError("Unable to resolve API hostname.")

        resolved_ips = {ipaddress.ip_address(address[4][0]) for address in addresses}

        if not resolved_ips:
            raise ValueError("Hostname did not resolve to an IP address.")

        # Reject the hostname if ANY resolved address is non-public.
        for ip in resolved_ips:
            if not ip.is_global:
                raise ValueError(
                    "Requests to private or non-public IP addresses are not allowed."
                )

    except ValueError:
        raise
    except Exception:
        raise ValueError("Invalid URL.")


async def make_request(url: str, method="GET", body=None):

    await validate_url(url)

    # Redirects disabled to prevent redirect-based SSRF.
    async with httpx.AsyncClient(
        timeout=8.0,
        follow_redirects=False,
        trust_env=False,
    ) as client:

        async with client.stream(
            method,
            url,
            json=body,
        ) as response:

            chunks = []
            total_size = 0

            # Enforce response-size limit while streaming.
            async for chunk in response.aiter_bytes():

                total_size += len(chunk)

                if total_size > MAX_RESPONSE_SIZE:
                    raise ValueError("API response exceeds the 20 KB size limit.")

                chunks.append(chunk)

            raw = b"".join(chunks)

            try:
                data = json.loads(raw)
                is_json = True

            except (ValueError, UnicodeDecodeError):
                data = raw.decode("utf-8", errors="replace")
                is_json = False

            return {
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "is_json": is_json,
                "body": data,
            }


# Helper functions for configurable JSON field exclusions


def remove_path(data, parts):

    if not parts:
        return

    key = parts[0]

    if isinstance(data, dict):

        if len(parts) == 1:
            data.pop(key, None)

        elif key in data:
            remove_path(data[key], parts[1:])

    elif isinstance(data, list):

        if key == "*":

            for item in data:
                remove_path(item, parts[1:])

        elif key.isdigit():

            index = int(key)

            if index < len(data):
                remove_path(data[index], parts[1:])


def remove_ignored_paths(data, paths):

    for path in paths:

        parts = path.split(".")

        if not path or any(not part for part in parts):
            raise ValueError(f"Invalid JSON path: {path}")

        remove_path(data, parts)

    return data


# Tool 1: Test API


@mcp.tool()
async def test_api(
    url: str,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    """Test a publicly hosted API and return its response."""

    check_rate_limit()

    method = method.upper()

    if method not in {"GET", "POST"}:
        raise ValueError("Only GET and POST are supported.")

    return await make_request(url, method, body)


# Tool 2: Validate JSON


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

        validate(
            instance=result["body"],
            schema=schema,
        )

        return {
            "valid": True,
            "message": "Schema validation passed",
        }

    except ValidationError as exc:

        return {
            "valid": False,
            "error": exc.message,
            "path": list(exc.absolute_path),
        }


# Tool 3: Compare APIs


@mcp.tool()
async def compare_apis(
    url_a: str,
    url_b: str,
    ignore_paths: list[str] | None = None,
) -> dict:
    """
    Compare two API responses.

    Optionally ignore specified JSON fields
    using dotted paths.
    """

    check_rate_limit()

    result_a = await make_request(url_a)
    result_b = await make_request(url_b)

    json_match = False

    if result_a["is_json"] and result_b["is_json"]:

        body_a = copy.deepcopy(result_a["body"])
        body_b = copy.deepcopy(result_b["body"])

        paths = ignore_paths or []

        body_a = remove_ignored_paths(body_a, paths)
        body_b = remove_ignored_paths(body_b, paths)

        json_match = body_a == body_b

    return {
        "status_match": result_a["status"] == result_b["status"],
        "json_match": json_match,
        "ignored_paths": ignore_paths or [],
        "api_a": result_a,
        "api_b": result_b,
    }


# Tool 4: Generate Report


@mcp.tool()
async def generate_report(url: str) -> dict:
    """Generate a concise API test report."""

    check_rate_limit()

    result = await make_request(url)

    passed = 200 <= result["status"] < 300 and result["is_json"]

    return {
        "url": url,
        "status": result["status"],
        "is_json": result["is_json"],
        "passed": passed,
        "summary": (
            "API test passed" if passed else "API response needs investigation"
        ),
    }


if __name__ == "__main__":
    print("Starting MCP server...", flush=True)

    mcp.run(
        transport="http",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
    )
