"""Saving rooms so they survive a server restart.

Memory is still where games live while the server runs. The store is a
backup copy: every change is written to it, and on startup every saved room
is loaded back. Two implementations share one interface:
    RedisRoomStore   for the real server
    MemoryRoomStore  for tests, so most tests don't need Redis
"""

import logging
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis

from mafia_game.engine.models import GameState

logger = logging.getLogger(__name__)


class SavedRoom(BaseModel):
    """Everything needed to bring a room back after a restart."""

    state: GameState
    tokens: dict[str, str] = Field(default_factory=dict)  # token -> player id
    version: int = 0  # so phones never see the view version go backwards


class RoomStore(Protocol):
    async def save(self, room: SavedRoom) -> None: ...

    async def load_all(self) -> list[SavedRoom]: ...

    async def delete(self, room_code: str) -> None: ...

    async def close(self) -> None: ...


class MemoryRoomStore:
    """Keeps saved rooms in a dict. Stores JSON text, like Redis, so tests
    still check that a room survives being turned into JSON and back."""

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    async def save(self, room: SavedRoom) -> None:
        self.saved[room.state.room_code] = room.model_dump_json()

    async def load_all(self) -> list[SavedRoom]:
        return [SavedRoom.model_validate_json(raw) for raw in self.saved.values()]

    async def delete(self, room_code: str) -> None:
        self.saved.pop(room_code, None)

    async def close(self) -> None:
        pass


class RedisRoomStore:
    """One Redis key per room, holding the whole room as JSON, with an expiry."""

    def __init__(self, client: Redis, ttl_seconds: int, prefix: str = "mafia:room:") -> None:
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.prefix = prefix  # keeps our keys apart from anything else in the database

    def key(self, room_code: str) -> str:
        return self.prefix + room_code

    async def save(self, room: SavedRoom) -> None:
        # One SET writes the whole room at once and resets its expiry.
        await self.client.set(
            self.key(room.state.room_code), room.model_dump_json(), ex=self.ttl_seconds
        )

    async def load_all(self) -> list[SavedRoom]:
        rooms = []
        # SCAN walks the keys in small batches. Never use KEYS on a real server:
        # it blocks Redis until it has checked every key.
        async for key in self.client.scan_iter(match=self.prefix + "*"):
            raw = await self.client.get(key)
            if raw is None:
                continue  # expired between SCAN and GET
            try:
                rooms.append(SavedRoom.model_validate_json(raw))
            except ValidationError:
                logger.warning("Skipping unreadable saved room %s", key)
        return rooms

    async def delete(self, room_code: str) -> None:
        await self.client.delete(self.key(room_code))

    async def close(self) -> None:
        await self.client.aclose()
