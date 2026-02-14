import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 9131
DEFAULT_TIMEOUT = 300  # 5 minutes


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


def main():
    parser = argparse.ArgumentParser(
        prog="pokeme",
        description="Desktop notification tool for AI agents that need human input.",
    )
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

    args = parser.parse_args()

    if args.command == "ask":
        cmd_ask(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
