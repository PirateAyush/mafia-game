"""Rooms: live games in memory, connected to players' phones.

The engine knows the rules and nothing else. This layer adds what the engine
deliberately ignores: who is connected, locking, timers and sending views.

Every change to a room follows the same pattern, always inside the room's lock:
    1. apply a rule from the engine
    2. end the phase early if everyone has acted
    3. (re)start the phase timer if the phase changed
    4. send every connected player their own fresh view
"""

import asyncio
import random
import secrets
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import ValidationError

from mafia_game.engine.models import GameState, Settings
from mafia_game.engine.rules import (
    GameError,
    add_player,
    advance_phase,
    create_game,
    return_to_lobby,
    should_end_early,
    skip_discussion,
    start_game,
    submit_night_action,
    submit_vote,
    update_settings,
)
from mafia_game.engine.views import view_for
from mafia_game.ws.protocol import (
    CastVote,
    ClientMessage,
    NightAction,
    ReturnToLobby,
    SkipDiscussion,
    StartGame,
    UpdateSettings,
    parse_client_message,
)

ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L look-alikes
ROOM_CODE_LENGTH = 6

# WebSocket close codes 4000-4999 are free for apps to define.
CLOSE_NOT_FOUND = 4404  # bad room code or token: don't retry
CLOSE_REPLACED = 4409  # the same seat connected from another tab: don't retry


def now_ms() -> int:
    return int(time.time() * 1000)


class Connection(Protocol):
    """Anything we can send JSON to: a real WebSocket in production, a fake in tests."""

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


class Room:
    def __init__(self, state: GameState) -> None:
        self.state = state
        self.lock = asyncio.Lock()
        self.connections: dict[str, Connection] = {}  # player id -> their open socket
        self.version = 0  # goes up with every view we send
        self.timer: asyncio.Task[None] | None = None
        self.timer_phase_id: int | None = None  # the phase the current timer belongs to


class RoomManager:
    def __init__(self, clock: Callable[[], int] = now_ms, rng: random.Random | None = None) -> None:
        self.clock = clock
        # SystemRandom uses the OS's secure randomness, so roles can't be predicted.
        self.rng = rng or random.SystemRandom()
        self.rooms: dict[str, Room] = {}
        self.tokens: dict[str, tuple[str, str]] = {}  # token -> (room code, player id)

    # ------------------------------------------------- joining (from HTTP)

    async def create_room(self, host_name: str, settings: Settings) -> tuple[str, str, str]:
        """Returns (room code, host's player id, host's token)."""
        code = self._new_room_code()
        player_id = _new_player_id()
        self.rooms[code] = Room(create_game(code, player_id, host_name, settings))
        return code, player_id, self._issue_token(code, player_id)

    async def join_room(self, code: str, name: str) -> tuple[str, str]:
        """Returns (player id, token)."""
        room = self.get_room(code)
        player_id = _new_player_id()
        async with room.lock:
            add_player(room.state, player_id, name)
            await self._broadcast(room)  # everyone in the lobby sees the new name
        return player_id, self._issue_token(room.state.room_code, player_id)

    def get_room(self, code: str) -> Room:
        room = self.rooms.get(code.upper())
        if room is None:
            raise GameError("ROOM_NOT_FOUND", "No room with that code")
        return room

    def find_player(self, code: str, token: str) -> tuple[Room, str] | None:
        seat = self.tokens.get(token)
        if seat is None or seat[0] != code.upper():
            return None
        return self.rooms[seat[0]], seat[1]

    # ----------------------------------------------- connections (from WS)

    async def connect(self, room: Room, player_id: str, conn: Connection) -> None:
        async with room.lock:
            old = room.connections.get(player_id)
            room.connections[player_id] = conn
            if old is not None:
                await _safe_close(old, CLOSE_REPLACED)
            await self._broadcast(room)  # sends this player their view, shows them online

    async def disconnect(self, room: Room, player_id: str, conn: Connection) -> None:
        async with room.lock:
            # Only remove the seat's socket if it's still this one. When a player
            # reconnects, the old socket's cleanup runs after the new one connected.
            if room.connections.get(player_id) is conn:
                del room.connections[player_id]
                await self._broadcast(room)

    async def handle(self, room: Room, player_id: str, raw: str) -> None:
        """Handle one raw message from a player's phone."""
        try:
            message = parse_client_message(raw)
        except ValidationError:
            await self._send_error(room, player_id, "BAD_MESSAGE", "Message not understood")
            return

        async with room.lock:
            try:
                self._apply(room.state, player_id, message)
            except GameError as exc:
                await self._send_error(room, player_id, exc.code, exc.message)
                return
            await self._after_change(room)

    def _apply(self, state: GameState, player_id: str, message: ClientMessage) -> None:
        now = self.clock()
        match message:
            case StartGame():
                start_game(state, player_id, now, self.rng)
            case UpdateSettings(changes=changes):
                update_settings(state, player_id, changes)
            case NightAction(target=target, phase_id=phase_id):
                submit_night_action(state, player_id, target, phase_id)
            case CastVote(target=target, phase_id=phase_id):
                submit_vote(state, player_id, target, phase_id)
            case SkipDiscussion():
                skip_discussion(state, player_id, now)
            case ReturnToLobby():
                return_to_lobby(state, player_id, now)

    # ------------------------------------------------------ after a change

    async def _after_change(self, room: Room) -> None:
        """Steps 2 to 4 of the pattern above. Must be called while holding the lock."""
        if should_end_early(room.state):
            advance_phase(room.state, self.clock())
        self._schedule_timer(room)
        await self._broadcast(room)

    def _schedule_timer(self, room: Room) -> None:
        state = room.state
        if room.timer_phase_id == state.phase_id:
            return  # same phase, its timer is already running

        # Cancel the old phase's timer, unless the old timer is the code running
        # right now (a timer that just fired); cancelling it would stop itself.
        if room.timer is not None and room.timer is not asyncio.current_task():
            room.timer.cancel()

        room.timer_phase_id = state.phase_id
        if state.phase_ends_at is None:
            room.timer = None
        else:
            room.timer = asyncio.create_task(
                self._run_timer(room, state.phase_id, state.phase_ends_at)
            )

    async def _run_timer(self, room: Room, phase_id: int, ends_at: int) -> None:
        await asyncio.sleep(max(0, ends_at - self.clock()) / 1000)
        await self._on_timer(room, phase_id)

    async def _on_timer(self, room: Room, phase_id: int) -> None:
        async with room.lock:
            if room.state.phase_id != phase_id:
                return  # the phase already ended early, nothing to do
            advance_phase(room.state, self.clock())
            await self._after_change(room)

    async def _broadcast(self, room: Room) -> None:
        """Send every connected player their own view. Must be called while holding the lock."""
        room.version += 1
        now = self.clock()
        online = set(room.connections)
        sends = []
        for player_id, conn in room.connections.items():
            view = view_for(room.state, player_id, online)
            message = {
                "type": "view",
                "version": room.version,
                "server_time": now,
                "state": view.model_dump(mode="json"),
            }
            sends.append(_safe_send(conn, message))
        await asyncio.gather(*sends)  # send to all phones at the same time, not one by one

    async def _send_error(self, room: Room, player_id: str, code: str, message: str) -> None:
        conn = room.connections.get(player_id)
        if conn is not None:
            await _safe_send(conn, {"type": "error", "code": code, "message": message})

    # ------------------------------------------------------------- helpers

    def _new_room_code(self) -> str:
        while True:
            code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))
            if code not in self.rooms:
                return code

    def _issue_token(self, code: str, player_id: str) -> str:
        token = secrets.token_urlsafe(24)
        self.tokens[token] = (code, player_id)
        return token

    async def close(self) -> None:
        """Stop all timers. Called when the server shuts down."""
        timers = [room.timer for room in self.rooms.values() if room.timer is not None]
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)


def _new_player_id() -> str:
    return "p_" + secrets.token_hex(4)


async def _safe_send(conn: Connection, message: dict[str, Any]) -> None:
    # A phone can drop at any moment. One dead socket must not break the
    # broadcast for everyone else; its own disconnect() cleans it up.
    try:
        await conn.send_json(message)
    except Exception:
        pass


async def _safe_close(conn: Connection, code: int) -> None:
    try:
        await conn.close(code=code)
    except Exception:
        pass
