"""onsense CLI — dispatch for the serve / pair / doctor subcommands."""
import argparse
import sys

from . import __version__

try:  # Make Unicode/emoji output safe on the Windows console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="onsense",
        description="Turn your phone into the AI's eyes and sensors — PC-side MCP broker (onSense)",
    )
    p.add_argument("-V", "--version", action="version",
                   version=f"onsense {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="{pair,doctor,serve,clip}")

    sp = sub.add_parser("serve", help="Run the MCP server (stdio). Called by the AI client — registered automatically by pair")
    sp.add_argument("--token", help="Pairing token shown by the phone (default: read from ~/.onsense/pair.json)")
    sp.add_argument("--no-clip", action="store_true",
                    help="Do not auto-start the phone→PC clipboard/file daemon")

    cp = sub.add_parser("clip", help="Run the phone↔PC clipboard/file daemon (:8770). serve auto-starts it, but you can run it standalone")
    cp.add_argument("--port", type=int, default=None, help="Listening port (default 8770)")
    cp.add_argument("--allow-pull", action="store_true",
                    help="Allow the phone to pull the PC clipboard/copied files via GET /clip (persistent setting ON)")
    cp.add_argument("--no-allow-pull", action="store_true",
                    help="Turn GET /clip off persistently (also applied immediately to a running daemon)")
    cp.add_argument("--set-clipboard", action="store_true",
                    help="Allow auto-injecting into the OS clipboard when a phone→PC POST arrives (persistent setting ON; files are always saved)")
    cp.add_argument("--no-set-clipboard", action="store_true",
                    help="Turn OS clipboard auto-injection off persistently (also applied immediately to a running daemon)")
    cp.add_argument("--allow-remote-settings", action="store_true",
                    help="Approve once so the phone app settings can remotely change pull permission (POST /settings) (persistent ON)")
    cp.add_argument("--no-remote-settings", action="store_true",
                    help="Turn phone remote-settings approval off persistently (also applied immediately to a running daemon)")

    pp = sub.add_parser("pair", help="Pair with the phone + auto-register the MCP server with Claude")
    pp.add_argument("uri", nargs="?",
                    help="onsense://pair?base=...&token=... (omit for QR listener mode)")
    pp.add_argument("--img", metavar="PNG", help="Decode from a QR image file (requires opencv)")
    pp.add_argument("--local", action="store_true",
                    help="For development: register with the current interpreter (python -m onsense) instead of uvx")
    pp.add_argument("--client", default="claude",
                    help="Target CLI for MCP registration (default: claude)")

    dp = sub.add_parser("doctor", help="Diagnose install/connection problems (Python/uv/mcp/Claude/network/phone)")
    dp.add_argument("--base", help="Phone address http://IP:8080 (omit for automatic mDNS discovery)")
    dp.add_argument("--token", help="Pairing token shown by the phone")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        import os
        if getattr(args, "token", None):
            os.environ["PHONE_TOKEN"] = args.token
        if not getattr(args, "no_clip", False):
            from . import clip
            clip.spawn_detached()  # Ensure the phone→PC clipboard/file daemon singleton (a failure does not affect serve)
        from . import server
        server.main()
        return 0
    if args.cmd == "clip":
        from . import clip
        # If any flag is set, update the persistent state (settings.json) first and print the result.
        # Then call clip.main() — if a daemon is already running it exits with "already running", but
        # the live daemon reads the persistent settings LIVE at request time, so the change still takes effect (option ①).
        allow_pull = True if args.allow_pull else (False if args.no_allow_pull else None)
        set_clipboard = True if args.set_clipboard else (False if args.no_set_clipboard else None)
        allow_remote = True if args.allow_remote_settings else (False if args.no_remote_settings else None)
        if allow_pull is not None or set_clipboard is not None or allow_remote is not None:
            state = clip.save_flags(allow_pull=allow_pull, set_clipboard=set_clipboard,
                                    allow_remote_settings=allow_remote)
            print(f"[clip] persistent settings updated → pull={'ON' if state['allow_pull'] else 'OFF'}, "
                  f"set_clipboard={'ON' if state['set_clipboard'] else 'OFF'}, "
                  f"remote_settings={'ON' if state['allow_remote_settings'] else 'OFF'}")
        # main() args are for persistent promotion (only True is meaningful); anything else is None and keeps the existing persistent value.
        return clip.main(args.port or clip.CLIP_PORT,
                         bool(args.allow_pull), bool(args.set_clipboard))
    if args.cmd == "pair":
        from . import pair
        return pair.main(args)
    if args.cmd == "doctor":
        from . import doctor
        return doctor.main(args)
    build_parser().print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
