#!/usr/bin/env python3
"""CTF Agent — 统一入口点。

可同时启动核心引擎和仪表盘：
    python main.py                          # 仅启动引擎
    python main.py --dashboard              # 引擎 + 仪表盘
    python main.py --dashboard-only         # 仅仪表盘

通过 --help 查看所有选项。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CTF Agent — autonomous CTF solver with human-machine dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py                                        # Start engine only\n"
            "  python main.py --dashboard                            # Engine + dashboard\n"
            "  python main.py --dashboard-only                       # Dashboard only\n"
            "  python main.py --ctfd-url https://ctf.example.com     # Custom CTFd URL\n"
            "  python main.py --challenge single_challenge           # Solve one challenge\n"
        ),
    )

    # Engine options
    engine = parser.add_argument_group("Engine Options")
    engine.add_argument("--ctfd-url", default=None, help="CTFd URL (overrides .env)")
    engine.add_argument("--ctfd-token", default=None, help="CTFd API token (overrides .env)")
    engine.add_argument("--coordinator", default="claude", choices=["claude", "codex"],
                        help="Coordinator backend (default: claude)")
    engine.add_argument("--challenge", default=None,
                        help="Solve a single challenge directory (skip coordinator)")
    engine.add_argument("--challenges-dir", default="challenges",
                        help="Directory for challenge files (default: challenges)")
    engine.add_argument("--max-challenges", default=10, type=int,
                        help="Max concurrent challenges (default: 10)")
    engine.add_argument("--models", nargs="*", default=[],
                        help="Model specs to use (default: all configured)")
    engine.add_argument("--image", default="ctf-sandbox",
                        help="Docker sandbox image name (default: ctf-sandbox)")
    engine.add_argument("--no-submit", action="store_true",
                        help="Dry run — don't submit flags")
    engine.add_argument("--msg-port", default=0, type=int,
                        help="Operator message port (0 = auto)")
    engine.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")

    # Dashboard options
    dashboard = parser.add_argument_group("Dashboard Options")
    dashboard.add_argument("--dashboard", action="store_true",
                           help="Launch the Streamlit dashboard alongside the engine")
    dashboard.add_argument("--dashboard-only", action="store_true",
                           help="Launch only the dashboard (engine must be running separately)")
    dashboard.add_argument("--dashboard-port", default=8501, type=int,
                           help="Dashboard port (default: 8501)")

    return parser.parse_args()


def _start_dashboard(port: int) -> threading.Thread:
    """Start the Streamlit dashboard in a background thread."""
    dashboard_path = Path(__file__).parent / "dashboard" / "app.py"

    def _run() -> None:
        cmd = [
            sys.executable, "-m", "streamlit", "run",
            str(dashboard_path),
            f"--server.port={port}",
            "--server.headless=true",
            "--server.runOnSave=false",
            "--client.toolbarMode=minimal",
        ]
        env = {"COORDINATOR_MSG_PORT": str(args.msg_port) if args.msg_port else "9400"}
        subprocess.run(cmd, env={**__import__("os").environ, **env})

    thread = threading.Thread(target=_run, daemon=True, name="dashboard")
    thread.start()
    return thread


def _run_engine(args: argparse.Namespace) -> None:
    """Run the CTF solving engine via CLI entry point."""
    from backend.cli import main

    # Build click-compatible args
    click_args = ["ctf-solve"]
    if args.ctfd_url:
        click_args.extend(["--ctfd-url", args.ctfd_url])
    if args.ctfd_token:
        click_args.extend(["--ctfd-token", args.ctfd_token])
    if args.challenge:
        click_args.extend(["--challenge", args.challenge])
    if args.challenges_dir:
        click_args.extend(["--challenges-dir", args.challenges_dir])
    if args.coordinator:
        click_args.extend(["--coordinator", args.coordinator])
    if args.max_challenges:
        click_args.extend(["--max-challenges", str(args.max_challenges)])
    if args.models:
        for m in args.models:
            click_args.extend(["--models", m])
    if args.image:
        click_args.extend(["--image", args.image])
    if args.no_submit:
        click_args.append("--no-submit")
    if args.msg_port:
        click_args.extend(["--msg-port", str(args.msg_port)])
    if args.verbose:
        click_args.append("-v")

    import sys
    sys.argv = click_args
    main()


def main() -> None:
    """Main entry point."""
    global args
    args = _parse_args()

    if args.dashboard_only:
        # Start only the dashboard
        port = args.dashboard_port
        print(f"🚀 Starting dashboard on port {port}...")
        print(f"📊 Open http://localhost:{port} in your browser")
        print("⚠️  Make sure the engine is running separately!")
        _start_dashboard(port)
        # Keep main thread alive
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\n👋 Dashboard stopped.")
        return

    if args.dashboard:
        # Start dashboard in background, then engine
        port = args.dashboard_port or 8501
        print(f"🚀 Starting dashboard on port {port}...")
        _start_dashboard(port)
        print(f"📊 Dashboard: http://localhost:{port}")

    # Run the engine
    try:
        _run_engine(args)
    except KeyboardInterrupt:
        print("\n👋 Engine stopped.")
    except Exception as e:
        print(f"❌ Engine error: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
