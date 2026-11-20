# Mafia

![CI](https://github.com/PirateAyush/mafia-game/actions/workflows/ci.yml/badge.svg)

A real-time Mafia party game. Every player uses their own phone, and the server runs the game.

Built with Python, FastAPI and WebSockets.

## Run locally

```bash
uv sync
uv run uvicorn mafia_game.main:app --reload
```

Open http://localhost:8000/debug in several browser tabs, one tab per player.

## Checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```
