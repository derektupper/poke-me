# pokeme

![Screenshot of PokeMe in action](image.png)

A CLI tool that lets AI coding agents ask you questions and wait for your answer. When an agent needs input, it sends a browser notification and displays the question in a local web UI.

Zero dependencies. Pure Python stdlib. Works with any agent that can run shell commands.

## Install

```bash
pip install .
```

Or in editable/dev mode:

```bash
pip install -e .
```

## Quick start

From any terminal (or from an agent's shell):

```bash
pokeme ask "Which database should I use?"
```

What happens:
1. A local web UI starts at **http://localhost:9131**
2. If the page is open, a browser notification pops up: *"pokeme — Agent needs input"*
3. You type your answer, hit Send
4. The CLI prints your answer to stdout and exits

The agent reads stdout and continues working.

## CLI usage

### Ask a question

```bash
pokeme ask "What should I name this module?"
```

With more detail:

```bash
pokeme ask "Should I use REST or GraphQL?" \
  --agent "backend-agent" \
  --task "Designing the API layer" \
  --context "We have 12 endpoints, mostly CRUD. The frontend team prefers GraphQL but we have no experience with it." \
  --timeout 120
```

| Flag | Short | Description |
|---|---|---|
| `--agent` | `-a` | Name of the agent asking (shown in the web UI) |
| `--task` | `-t` | What the agent is working on |
| `--context` | `-c` | Extra detail beyond the question |
| `--timeout` | | Seconds to wait before giving up (default: 300) |
| `--port` | | Server port (default: 9131) |

### Check pending questions

```bash
pokeme status
```

Output:

```
  [backend-agent] (12s ago) Which database should I use?
  [frontend-agent] (3s ago) Dark mode or light mode?
```

### Open the web UI

```bash
pokeme open
```

Starts the server if needed and opens `http://localhost:9131` in your default browser.

### Stop the server

```bash
pokeme stop
```

Gracefully shuts down the background server. The server also stops itself automatically after 10 minutes of inactivity.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Got an answer (printed to stdout) |
| 1 | Timed out or server error |

## Multi-agent support

Multiple agents can ask questions at the same time. Each runs `pokeme ask` in its own process. The web UI shows all pending questions, labeled by agent name and task. Each agent gets back only its own answer.

```
Terminal 1:  pokeme ask "Which DB?" --agent "backend"
Terminal 2:  pokeme ask "Dark mode?" --agent "frontend"
Terminal 3:  pokeme ask "Deploy to staging?" --agent "devops"
```

All three appear in the web UI at `http://localhost:9131`. Answer them in any order.

## Hooking it up to your agent

### Any agent that can run shell commands

The interface is stdin/stdout. If your agent can shell out, it can use pokeme:

```bash
answer=$(pokeme ask "Which testing framework?" --agent "my-agent")
echo "User said: $answer"
```

### Claude Code

Add to your `CLAUDE.md`:

```
When you need human input for a decision, run:
pokeme ask "your question" --agent "claude-code" --task "description of current work"
Read the stdout response and use it to continue.
```

### Python agents (LangChain, CrewAI, custom)

```python
import subprocess

def ask_human(question, context=""):
    result = subprocess.run(
        ["pokeme", "ask", question, "--agent", "my-agent", "--context", context],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise TimeoutError("No response from user")
    return result.stdout.strip()
```

### As an MCP tool

Wrap the CLI call in an MCP tool definition so any MCP-compatible agent can call it directly.

## Building a standalone binary

pokeme can be packaged as a single executable using [PyInstaller](https://pyinstaller.org/) so users don't need Python installed.

```bash
pip install pyinstaller
pyinstaller --onefile --name pokeme src/pokeme/cli.py
```

The binary is output to `dist/pokeme` (or `dist/pokeme.exe` on Windows).

**Note:** PyInstaller can only build for the OS it runs on. To build for Linux, run the command on a Linux machine (or use WSL/Docker/CI).

## How it works under the hood

- First `pokeme ask` auto-starts a lightweight HTTP server on localhost:9131
- The server holds all pending questions in memory (no database, no files)
- The CLI posts a question via the REST API, then polls for the answer
- The web UI polls the server every 2 seconds, displays pending question cards with agent avatars, and fires browser notifications for new requests
- When you submit an answer, the CLI picks it up and prints it to stdout
- The server shuts itself down after 10 minutes of no pending questions
- Answered requests are evicted from memory after 5 minutes

### Security

- All traffic is localhost-only — the server binds to `127.0.0.1`
- CORS restricted to localhost origins
- Input validation and field length limits on all endpoints (questions: 2000 chars, answers: 10,000 chars)
- Request body size capped at 64 KB
- Maximum 100 pending requests at a time
- Request IDs validated against a strict hex pattern

## Notifications

pokeme uses the **browser Notification API** to alert you when agents need input. On first visit to the web UI, you'll see a banner asking to enable notifications. Once enabled, you'll get browser notifications even when the tab is in the background.

**Tip:** Keep `http://localhost:9131` open in a browser tab. When any agent sends a question, you'll get a notification — click it to jump straight to the answer form.

## Running tests

```bash
pytest tests/
```

## Requirements

- Python 3.9+
- No external dependencies
