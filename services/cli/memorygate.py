#!/usr/bin/env python3
"""Portable MemoryGate CLI for terminal-capable agents."""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_URL_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


def request(method: str, path: str, payload: dict | None = None) -> dict:
    base = os.getenv("MEMORYGATE_URL", "http://127.0.0.1:8020").rstrip("/")
    key = os.getenv("MEMORYGATE_KEY", "")
    agent_id = os.getenv("MEMORYGATE_AGENT_ID", "default")
    if not key:
        raise RuntimeError("MEMORYGATE_KEY is required")
    request = Request(f"{base}{path}", data=json.dumps(payload).encode() if payload is not None else None, method=method, headers={"Content-Type": "application/json", "X-MemoryGate-Key": key, "X-Agent-Id": agent_id})
    with _URL_OPENER.open(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(prog="memorygate")
    sub = parser.add_subparsers(dest="command", required=True)
    context = sub.add_parser("context", help="retrieve bounded, read-only context")
    context.add_argument("query")
    context.add_argument("--evidence", action="store_true")
    context.add_argument("--limit", type=int, default=12)
    ingest = sub.add_parser("ingest", help="submit trusted listener evidence; requires admin key")
    ingest.add_argument("source_key")
    ingest.add_argument("content")
    ingest.add_argument("--title", default="")
    ingest.add_argument("--tag", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "context":
            result = request("POST", "/runtime/context", {"query": args.query, "max_items": args.limit, "include_evidence": args.evidence})
        else:
            result = request("POST", "/runtime/ingest", {"source_key": args.source_key, "content": args.content, "title": args.title, "tags": args.tag, "auto_process": True})
        print(json.dumps(result, indent=2))
        return 0
    except (RuntimeError, HTTPError, URLError) as exc:
        print(f"memorygate: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
