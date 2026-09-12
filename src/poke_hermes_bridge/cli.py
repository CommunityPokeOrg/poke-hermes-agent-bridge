"""poke-hermes-bridge command line interface."""

import argparse
import asyncio
import secrets
import sys
from urllib.parse import quote

from . import __version__
from .config import Settings


def _load_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # noqa: BLE001
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = _load_settings()
    problems = settings.validate_startup()
    if problems:
        for p in problems:
            print(f"config error: {p}", file=sys.stderr)
        return 2
    from .app import create_app

    app = create_app(settings)
    uvicorn.run(app, host=settings.bridge_host, port=settings.bridge_port)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .hermes.client import HermesClient, HermesError

    settings = _load_settings()
    problems = settings.validate_startup()
    for p in problems:
        print(f"config error: {p}", file=sys.stderr)
    if problems:
        return 2

    async def _probe() -> int:
        client = HermesClient(
            base_url=settings.hermes_base_url,
            api_key=settings.hermes_api_key,
            timeout=settings.hermes_timeout_seconds,
            verify_tls=settings.hermes_verify_tls,
        )
        try:
            try:
                health = await client.health()
            except HermesError as exc:
                print(f"hermes: UNREACHABLE ({exc})")
                return 1
            print(f"hermes: reachable ({health.get('version', '?')})")
            try:
                caps = await client.capabilities()
                for k, v in caps.items():
                    print(f"  {k}: {v}")
            except HermesError as exc:
                print(f"  capabilities: error ({exc})")
                return 1
            return 0
        finally:
            await client.aclose()

    return asyncio.run(_probe())


def cmd_gen_key(args: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(32))
    return 0


def cmd_poke_link(args: argparse.Namespace) -> int:
    settings = _load_settings()
    if not settings.bridge_public_url:
        print("BRIDGE_PUBLIC_URL is not set", file=sys.stderr)
        return 2
    url = settings.bridge_public_url.rstrip("/") + "/mcp"
    print(f"https://poke.com/integrations/new?name=Hermes&url={quote(url, safe='')}")
    return 0


def cmd_mock_hermes(args: argparse.Namespace) -> int:
    from .devtools.mock_hermes import run_mock

    run_mock(host=args.host, port=args.port, api_key=args.api_key)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poke-hermes-bridge")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the bridge server").set_defaults(func=cmd_serve)
    sub.add_parser("check", help="probe Hermes health + capabilities").set_defaults(func=cmd_check)
    sub.add_parser("gen-key", help="print a fresh API key").set_defaults(func=cmd_gen_key)
    sub.add_parser("poke-link", help="print a prefilled poke.com integration URL").set_defaults(
        func=cmd_poke_link
    )

    p_mock = sub.add_parser("mock-hermes", help="run a local mock Hermes API server")
    p_mock.add_argument("--host", default="127.0.0.1")
    p_mock.add_argument("--port", type=int, default=8642)
    p_mock.add_argument("--api-key", default="mock-key-0123456789ab")
    p_mock.set_defaults(func=cmd_mock_hermes)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
