"""Published ``kgbcia-mcp`` command-line entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Settings
from .server import run
from .services.neo4j_service import Neo4jService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KGB-CIA read-only MCP server")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--enable-cypher", action=argparse.BooleanOptionalAction, default=None)
    serve.add_argument("--artifacts", help="artifact directory or public HTTP(S) tarball")

    hubs = subparsers.add_parser("hubs", help="compute highest-degree path-search hubs")
    hubs.add_argument("--top", type=int, required=True)
    hubs.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "serve":
        settings = Settings.from_env({"artifacts": args.artifacts})
        run(
            args.transport,
            args.host,
            args.port,
            settings=settings,
            enable_cypher=args.enable_cypher,
        )
        return 0
    if args.command == "hubs":
        return _write_hubs(args.top, args.output)
    parser.error(f"unknown command: {args.command}")
    return 2


def _write_hubs(top: int, output: Path) -> int:
    settings = Settings.from_env()
    settings.require_neo4j()
    service = Neo4jService(
        settings.neo4j_uri or "",
        settings.neo4j_user or "",
        settings.neo4j_password or "",
        settings.neo4j_database,
    )
    try:
        hubs = service.compute_hubs(top)
    finally:
        service.close()
    output.mkdir(parents=True, exist_ok=True)
    path = output / "hubs.json"
    path.write_text(json.dumps({"hubs": hubs}, indent=2) + "\n")
    print(f"wrote {len(hubs)} hubs to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
