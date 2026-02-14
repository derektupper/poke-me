# pokeme

A CLI tool that lets AI coding agents ask you questions. When an agent needs input, it pops a desktop notification and waits for your answer.

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
1. A desktop notification appears: *"pokeme: An agent needs your help"*
2. A local web UI starts at **http://localhost:9131**
3. You open the page, type your answer, hit Send
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

## How it works under the hood

- First `pokeme ask` auto-starts a lightweight HTTP server on localhost:9131
- The server holds all pending questions in memory (no database, no files)
- The CLI posts a question to the server, fires a desktop toast notification, then polls for the answer
- The web UI polls the server every 2 seconds and displays pending question cards
- When you submit an answer, the CLI picks it up and prints it to stdout
- The server shuts itself down after 10 minutes of no pending questions

## Platform support

Desktop notifications use OS-native mechanisms:
- **Windows** -- PowerShell toast notifications
- **macOS** -- `osascript` / Notification Center
- **Linux** -- `notify-send`

If notifications fail (e.g. headless server), the URL is printed to stderr as a fallback.

## Requirements

- Python 3.9+
- No external dependencies
