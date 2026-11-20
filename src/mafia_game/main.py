"""HTTP and WebSocket entry points.

Kept thin on purpose: routes only translate between the network and the
RoomManager, which does the real work.

Run locally with:  uv run uvicorn mafia_game.main:app --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from mafia_game.engine.models import Settings
from mafia_game.engine.rules import GameError
from mafia_game.rooms.manager import CLOSE_NOT_FOUND, RoomManager

STATIC_DIR = Path(__file__).parent / "static"


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


def create_app(manager: RoomManager | None = None) -> FastAPI:
    rooms = manager or RoomManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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

    @app.websocket("/ws/{code}")
    async def game_socket(websocket: WebSocket, code: str, token: str) -> None:
        await websocket.accept()
        found = rooms.find_player(code, token)
        if found is None:
            # Accept first, then close with our own code, so the phone knows
            # not to keep retrying. Rejecting before accept looks like a network error.
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
