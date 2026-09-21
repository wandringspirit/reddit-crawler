"""Console entry point: start the local server and open the panel in the browser."""
from __future__ import annotations

import argparse
import socket
import threading
import webbrowser

import uvicorn

from .app import create_app
from .config import DEFAULT_PORT, data_dir, env_path


def _free_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port found near {preferred}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reddit-crawler", description="Local Reddit keyword crawler panel")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    args = parser.parse_args(argv)

    port = _free_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"
    print(f"Reddit Crawler panel: {url}")
    print(f"  data:  {data_dir()}")
    print(f"  env:   {env_path()} (optional Reddit API credentials)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
