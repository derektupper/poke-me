import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from pokeme import __version__

DEFAULT_PORT = 9131
DEFAULT_TIMEOUT = 300  # 5 minutes
GITHUB_REPO = "derektupper/poke-me"


def _server_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _is_server_running(port: int) -> bool:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
        sock.close()
        return True
    except OSError:
        return False


def _start_server(port: int) -> None:
    """Start the pokeme server as a detached background process."""
    kwargs: dict = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = CREATE_NO_WINDOW | DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(
        [sys.executable, "-m", "pokeme.server", "--port", str(port)],
        **kwargs,
    )
    # Wait for server to be ready
    for _ in range(50):
        if _is_server_running(port):
            return
        time.sleep(0.1)
    print("pokeme: warning: server may not have started", file=sys.stderr)


def _ensure_server(port: int) -> None:
    if not _is_server_running(port):
        _start_server(port)


def _api_post(port: int, path: str, data: dict) -> dict:
    url = f"{_server_url(port)}{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _api_get(port: int, path: str) -> dict:
    url = f"{_server_url(port)}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def _github_api(path: str) -> dict:
    """Make a GET request to the GitHub API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}{path}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "pokeme-updater",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _get_asset_name() -> str:
    """Return the expected release asset name for this platform."""
    if sys.platform == "win32":
        return "pokeme-windows-amd64.exe"
    return "pokeme-linux-amd64"


def _get_current_binary() -> str | None:
    """Return the path to the currently running binary, or None if not frozen."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def cmd_ask(args):
    port = args.port
    _ensure_server(port)

    # Create the request on the server
    payload = {"question": args.question}
    if args.context:
        payload["context"] = args.context
    if args.agent:
        payload["agent"] = args.agent
    if args.task:
        payload["task"] = args.task

    try:
        result = _api_post(port, "/api/ask", payload)
    except Exception as e:
        print(f"pokeme: failed to reach server: {e}", file=sys.stderr)
        sys.exit(1)

    request_id = result["id"]
    print(f"pokeme: respond at {_server_url(port)}", file=sys.stderr)

    # Poll for answer
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            status = _api_get(port, f"/api/status/{request_id}")
            if status.get("status") == "answered":
                print(status["answer"])
                sys.exit(0)
        except Exception:
            pass
        time.sleep(1)

    print("pokeme: timed out waiting for answer", file=sys.stderr)
    sys.exit(1)


def cmd_status(args):
    port = args.port
    if not _is_server_running(port):
        print("No pokeme server running.")
        return

    try:
        pending = _api_get(port, "/api/pending")
    except Exception as e:
        print(f"pokeme: failed to reach server: {e}", file=sys.stderr)
        sys.exit(1)

    if not pending:
        print("No pending requests.")
        return

    for req in pending:
        agent = req.get("agent") or "unknown"
        question = req["question"]
        age = int(time.time() - req["created_at"])
        print(f"  [{agent}] ({age}s ago) {question}")


def cmd_update(args):
    target_version = args.version

    # Fetch release info
    try:
        if target_version:
            release = _github_api(f"/releases/tags/v{target_version}")
        else:
            release = _github_api("/releases/latest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            label = f"v{target_version}" if target_version else "latest"
            print(f"pokeme: release {label} not found", file=sys.stderr)
        else:
            print(f"pokeme: failed to check for updates: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"pokeme: failed to check for updates: {e}", file=sys.stderr)
        sys.exit(1)

    release_version = release["tag_name"].lstrip("v")

    if release_version == __version__ and not target_version:
        print(f"pokeme: already up to date (v{__version__})")
        return

    # Find the right asset for this platform
    asset_name = _get_asset_name()
    asset_url = None
    for asset in release.get("assets", []):
        if asset["name"] == asset_name:
            asset_url = asset["browser_download_url"]
            break

    if not asset_url:
        print(f"pokeme: no {asset_name} found in release v{release_version}", file=sys.stderr)
        sys.exit(1)

    # Figure out where to write the binary
    current_binary = _get_current_binary()
    if not current_binary:
        print(f"pokeme: not running as a standalone binary — use pip install to update instead", file=sys.stderr)
        print(f"  pip install --upgrade git+https://github.com/{GITHUB_REPO}.git", file=sys.stderr)
        sys.exit(1)

    print(f"pokeme: updating v{__version__} -> v{release_version} ...")

    # Download to a temp file in the same directory (so rename is atomic)
    binary_dir = os.path.dirname(current_binary)
    try:
        req = urllib.request.Request(asset_url, headers={"User-Agent": "pokeme-updater"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            fd, tmp_path = tempfile.mkstemp(dir=binary_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
            except Exception:
                os.unlink(tmp_path)
                raise
    except Exception as e:
        print(f"pokeme: download failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Replace the current binary
    try:
        if sys.platform == "win32":
            # Windows can't overwrite a running exe, so rename the old one first
            old_path = current_binary + ".old"
            if os.path.exists(old_path):
                os.unlink(old_path)
            os.rename(current_binary, old_path)
            os.rename(tmp_path, current_binary)
            # Clean up old binary (best effort)
            try:
                os.unlink(old_path)
            except OSError:
                pass  # may still be locked, will be cleaned up next update
        else:
            os.chmod(tmp_path, os.stat(tmp_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.replace(tmp_path, current_binary)
    except Exception as e:
        print(f"pokeme: failed to replace binary: {e}", file=sys.stderr)
        # Try to clean up
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        sys.exit(1)

    print(f"pokeme: updated to v{release_version}")


def main():
    parser = argparse.ArgumentParser(
        prog="pokeme",
        description="Notification tool for AI agents that need human input.",
    )
    parser.add_argument("--version", "-V", action="version", version=f"pokeme {__version__}")
    sub = parser.add_subparsers(dest="command")

    # Shared arguments
    port_kwargs = dict(type=int, default=DEFAULT_PORT, help="Server port (default: 9131)")

    # ask
    ask_parser = sub.add_parser("ask", help="Ask the user a question and wait for an answer")
    ask_parser.add_argument("question", help="The question to ask")
    ask_parser.add_argument("--context", "-c", help="Additional context for the question")
    ask_parser.add_argument("--agent", "-a", help="Name of the agent asking")
    ask_parser.add_argument("--task", "-t", help="Description of what the agent is working on")
    ask_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Seconds to wait (default: 300)")
    ask_parser.add_argument("--port", **port_kwargs)

    # status
    status_parser = sub.add_parser("status", help="Show pending requests")
    status_parser.add_argument("--port", **port_kwargs)

    # update
    update_parser = sub.add_parser("update", help="Update pokeme to the latest version")
    update_parser.add_argument("--version", dest="version", default=None,
                               help="Specific version to install (e.g. 0.2.0)")

    args = parser.parse_args()

    if args.command == "ask":
        cmd_ask(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "update":
        cmd_update(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
