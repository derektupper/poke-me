# pokeme

A CLI tool and HTTP server that lets AI coding agents ask humans for input in real-time via browser notifications and a local web UI.

## Project Structure

- `src/pokeme/cli.py` — CLI entry point (`pokeme ask`, `pokeme status`)
- `src/pokeme/server.py` — Threaded HTTP server with embedded web UI
- `tests/` — Unit and integration tests (pytest)
- `pyproject.toml` — Package metadata and config

## Development

- Python 3.9+, zero external dependencies (stdlib only)
- Install in dev mode: `pip install -e .`
- Run tests: `pytest tests/`
- Server runs on `http://localhost:9131`

## Architecture

- In-memory storage only (no database) with thread-safe `RequestStore`
- Server runs as a detached background process, auto-shuts down after 10 min idle
- CLI auto-starts the server if not running
- Platform-aware process spawning (Windows vs Unix)
- Security: input validation, field truncation, CORS restricted to localhost
