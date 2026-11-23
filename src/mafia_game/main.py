"""HTTP and WebSocket entry points.

Kept thin on purpose: routes only translate between the network and the
RoomManager, which does the real work.

Run locally with:  uv run uvicorn mafia_game.main:app --reload
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis

from mafia_game.config import AppConfig
from mafia_game.engine.models import Settings
from mafia_game.engine.rules import GameError
from mafia_game.rooms.manager import CLOSE_NOT_FOUND, RoomManager
from mafia_game.rooms.store import RedisRoomStore
from mafia_game.ws.protocol import Auth

STATIC_DIR = Path(__file__).parent / "static"

AUTH_TIMEOUT_SECONDS = 5  # a phone must say who it is this quickly after connecting
CLOSE_AUTH_TIMEOUT = 4408  # it didn't: the phone may simply retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
logger = logging.getLogger(__name__)


class CreateRoomRequest(BaseModel):
    name: str
    reveal_role_on_death: bool = False


class JoinRoomRequest(BaseModel):
    name: str


class SeatResponse(BaseModel):
    """Everything a phone needs to (re)connect to its seat. Keep the token secret."""

    room_code: str
    player_id: str
    token: str


def build_manager(config: AppConfig) -> RoomManager:
    """The real setup: rooms saved in Redis, settings from the environment."""
    client = Redis.from_url(config.redis_url, decode_responses=True)
    return RoomManager(
        store=RedisRoomStore(client, config.room_ttl_seconds),
        host_grace_seconds=config.host_grace_seconds,
        idle_room_seconds=config.idle_room_seconds,
    )


def create_app(manager: RoomManager | None = None) -> FastAPI:
    rooms = manager or build_manager(AppConfig())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        restored = await rooms.restore()
        logger.info("Restored %d room(s) from the store", restored)
        rooms.start()
        yield  # the server runs here
        await rooms.close()

    app = FastAPI(title="Mafia", lifespan=lifespan)

    @app.exception_handler(GameError)
    async def game_error(request: Request, exc: GameError) -> JSONResponse:
        status = 404 if exc.code == "ROOM_NOT_FOUND" else 400
        return JSONResponse(status_code=status, content={"code": exc.code, "message": exc.message})

    @app.post("/rooms")
    async def create_room(body: CreateRoomRequest) -> SeatResponse:
        settings = Settings(reveal_role_on_death=body.reveal_role_on_death)
        code, player_id, token = await rooms.create_room(body.name, settings)
        return SeatResponse(room_code=code, player_id=player_id, token=token)

    @app.post("/rooms/{code}/join")
    async def join_room(code: str, body: JoinRoomRequest) -> SeatResponse:
        player_id, token = await rooms.join_room(code, body.name)
        return SeatResponse(room_code=code.upper(), player_id=player_id, token=token)

    @app.get("/healthz")
    async def health() -> dict[str, object]:
        """Liveness check for Docker and uptime monitors: is the process up and responding?"""
        return {"status": "ok", "rooms": len(rooms.rooms)}

    @app.websocket("/ws/{code}")
    async def game_socket(websocket: WebSocket, code: str) -> None:
        # Accept first, then close with our own codes, so the phone knows why.
        # Rejecting before accept looks like a network error and it would retry forever.
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT_SECONDS)
        except WebSocketDisconnect:
            return  # left before saying who they are
        except TimeoutError:
            await websocket.close(code=CLOSE_AUTH_TIMEOUT)
            return

        try:
            found = rooms.find_player(code, Auth.model_validate_json(raw).token)
        except ValidationError:
            found = None
        if found is None:
            await websocket.close(code=CLOSE_NOT_FOUND)
            return

        room, player_id = found
        await rooms.connect(room, player_id, websocket)
        try:
            while True:
                await rooms.handle(room, player_id, await websocket.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            await rooms.disconnect(room, player_id, websocket)

    @app.get("/debug")
    async def debug_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "debug.html")

    return app


app = create_app()
