"""Rooms: live games in memory, connected to players' phones.

The engine knows the rules and nothing else. This layer adds what the engine
deliberately ignores: who is connected, locking, timers, saving and sending views.

Every change to a room follows the same pattern, always inside the room's lock:
    1. apply a rule from the engine
    2. end the phase early if everyone has acted
    3. (re)start the phase timer if the phase changed
    4. send every connected player their own fresh view
    5. save the room to the store (after sending, so players never wait on Redis)
"""

import asyncio
import logging
import random
import secrets
import time
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from pydantic import ValidationError

from mafia_game.engine.models import ChatMessage, GameState, Settings
from mafia_game.engine.rules import (
    GameError,
    add_player,
    advance_phase,
    create_game,
    return_to_lobby,
    send_mafia_chat,
    should_end_early,
    skip_discussion,
    start_game,
    submit_night_action,
    submit_vote,
    transfer_host,
    update_settings,
)
from mafia_game.engine.views import view_for
from mafia_game.rooms.ratelimit import TokenBucket
from mafia_game.rooms.store import MemoryRoomStore, RoomStore, SavedRoom
from mafia_game.ws.protocol import (
    CastVote,
    ClientMessage,
    MafiaChat,
    NightAction,
    ReturnToLobby,
    SkipDiscussion,
    StartGame,
    UpdateSettings,
    parse_client_message,
)

logger = logging.getLogger(__name__)

ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L look-alikes
ROOM_CODE_LENGTH = 6
SWEEP_EVERY_SECONDS = 60

# Each player may send a burst of 10 messages, then 2 per second on average.
RATE_LIMIT_BURST = 10
RATE_LIMIT_PER_SECOND = 2

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
    def __init__(
        self, state: GameState, tokens: dict[str, str] | None = None, version: int = 0
    ) -> None:
        self.state = state
        self.tokens: dict[str, str] = tokens or {}  # token -> player id (saved with the room)
        self.version = version  # goes up with every view we send
        self.lock = asyncio.Lock()
        self.connections: dict[str, Connection] = {}  # player id -> their open socket
        self.offline_since: dict[str, int] = {}  # player id -> when their phone dropped
        self.rate_limits: dict[str, TokenBucket] = {}  # player id -> their message allowance
        self.last_seen = 0  # last time a real player did something (timers don't count)
        self.timer: asyncio.Task[None] | None = None
        self.timer_phase_id: int | None = None  # the phase the current timer belongs to


class RoomManager:
    def __init__(
        self,
        clock: Callable[[], int] = now_ms,
        rng: random.Random | None = None,
        store: RoomStore | None = None,
        host_grace_seconds: int = 60,
        idle_room_seconds: int = 30 * 60,
    ) -> None:
        self.clock = clock
        # SystemRandom uses the OS's secure randomness, so roles can't be predicted.
        self.rng = rng or random.SystemRandom()
        self.store: RoomStore = store or MemoryRoomStore()
        self.host_grace_ms = host_grace_seconds * 1000
        self.idle_room_ms = idle_room_seconds * 1000
        self.rooms: dict[str, Room] = {}
        self.tokens: dict[str, tuple[str, str]] = {}  # token -> (room code, player id)
        # Background tasks we started. asyncio only keeps weak references to tasks,
        # so without this set a running task could be garbage-collected mid-way.
        self._background: set[asyncio.Task[None]] = set()

    # --------------------------------------------- startup and shutdown

    async def restore(self) -> int:
        """Load every saved room back into memory. Returns how many were loaded."""
        try:
            saved_rooms = await self.store.load_all()
        except Exception:
            logger.exception("Could not load saved rooms; starting with none")
            return 0

        now = self.clock()
        for saved in saved_rooms:
            code = saved.state.room_code
            room = Room(saved.state, saved.tokens, saved.version)
            room.last_seen = now  # give everyone time to reconnect before the sweeper acts
            room.offline_since = {player_id: now for player_id in saved.state.players}
            self.rooms[code] = room
            for token, player_id in saved.tokens.items():
                self.tokens[token] = (code, player_id)
            # If the phase should have ended while the server was down,
            # this timer fires straight away and moves the game on.
            self._schedule_timer(room)
        return len(saved_rooms)

    def start(self) -> None:
        """Start background housekeeping. Called once the server is running."""
        self._run_in_background(self._sweep_forever())

    async def close(self) -> None:
        """Stop all timers and background tasks. Called when the server shuts down."""
        tasks = [room.timer for room in self.rooms.values() if room.timer is not None]
        tasks += list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()

    # ------------------------------------------------- joining (from HTTP)

    async def create_room(self, host_name: str, settings: Settings) -> tuple[str, str, str]:
        """Returns (room code, host's player id, host's token)."""
        code = self._new_room_code()
        player_id = _new_player_id()
        room = Room(create_game(code, player_id, host_name, settings))
        room.last_seen = self.clock()
        self.rooms[code] = room
        token = self._issue_token(room, player_id)
        await self._save(room)
        return code, player_id, token

    async def join_room(self, code: str, name: str) -> tuple[str, str]:
        """Returns (player id, token)."""
        room = self.get_room(code)
        player_id = _new_player_id()
        async with room.lock:
            add_player(room.state, player_id, name)
            token = self._issue_token(room, player_id)
            room.last_seen = self.clock()
            await self._after_change(room)  # saves, and everyone sees the new name
        return player_id, token

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
            room.offline_since.pop(player_id, None)
            room.last_seen = self.clock()
            if old is not None:
                await _safe_close(old, CLOSE_REPLACED)
            # The host may have been gone a long time (e.g. after a restart) with
            # nobody online to take over. Now someone is, so check again.
            self._maybe_transfer_host(room)
            await self._after_change(room)  # sends this player their view, shows them online

    async def disconnect(self, room: Room, player_id: str, conn: Connection) -> None:
        async with room.lock:
            # Only remove the seat's socket if it's still this one. When a player
            # reconnects, the old socket's cleanup runs after the new one connected.
            if room.connections.get(player_id) is not conn:
                return
            del room.connections[player_id]
            room.offline_since[player_id] = self.clock()
            room.last_seen = self.clock()
            if player_id == room.state.host_id:
                self._run_in_background(self._watch_host(room))
            await self._broadcast(room)

    async def handle(self, room: Room, player_id: str, raw: str) -> None:
        """Handle one raw message from a player's phone."""
        # Checked first, before any parsing, so a flood costs us as little as possible.
        if not self._allow(room, player_id):
            await self._send_error(room, player_id, "RATE_LIMITED", "Too many messages, slow down")
            return
        try:
            message = parse_client_message(raw)
        except ValidationError:
            await self._send_error(room, player_id, "BAD_MESSAGE", "Message not understood")
            return

        async with room.lock:
            room.last_seen = self.clock()
            try:
                chat = self._apply(room.state, player_id, message)
            except GameError as exc:
                await self._send_error(room, player_id, exc.code, exc.message)
                return
            if chat is not None:
                await self._send_mafia_chat(room, chat)
                await self._save(room)
            else:
                await self._after_change(room)

    def _allow(self, room: Room, player_id: str) -> bool:
        now = self.clock()
        bucket = room.rate_limits.get(player_id)
        if bucket is None:
            bucket = TokenBucket(RATE_LIMIT_BURST, RATE_LIMIT_PER_SECOND, now)
            room.rate_limits[player_id] = bucket
        return bucket.allow(now)

    def _apply(
        self, state: GameState, player_id: str, message: ClientMessage
    ) -> ChatMessage | None:
        """Apply one message to the game. Returns the new chat message, if it was one."""
        now = self.clock()
        match message:
            case MafiaChat(text=text):
                return send_mafia_chat(state, player_id, text, now)
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
        return None

    # ------------------------------------------------------ host hand-over

    async def _watch_host(self, room: Room) -> None:
        await asyncio.sleep(self.host_grace_ms / 1000)
        await self._check_host(room)

    async def _check_host(self, room: Room) -> None:
        async with room.lock:
            if self._maybe_transfer_host(room):
                await self._after_change(room)

    def _maybe_transfer_host(self, room: Room) -> bool:
        """If the host has been offline longer than the grace period, hand the role
        to the first online player. Returns True if the host changed."""
        host_id = room.state.host_id
        offline_since = room.offline_since.get(host_id)
        if host_id in room.connections or offline_since is None:
            return False
        if self.clock() - offline_since < self.host_grace_ms:
            return False  # they may still come back
        online = [p for p in room.state.players if p in room.connections]
        if not online:
            return False  # nobody to hand over to; connect() checks again later
        transfer_host(room.state, online[0])
        return True

    # ---------------------------------------------------- idle-room sweeper

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_EVERY_SECONDS)
            await self.sweep()

    async def sweep(self) -> None:
        """Forget rooms with nobody connected and no player activity for a while."""
        now = self.clock()
        for code, room in list(self.rooms.items()):
            async with room.lock:
                if room.connections or now - room.last_seen < self.idle_room_ms:
                    continue
                self.rooms.pop(code, None)
                if room.timer is not None:
                    room.timer.cancel()
                for token in room.tokens:
                    self.tokens.pop(token, None)
            await self._store_call(self.store.delete(code), f"delete room {code}")

    # ------------------------------------------------------ after a change

    async def _after_change(self, room: Room) -> None:
        """Steps 2 to 5 of the pattern above. Must be called while holding the lock."""
        if should_end_early(room.state):
            advance_phase(room.state, self.clock())
        self._schedule_timer(room)
        await self._broadcast(room)
        await self._save(room)

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

    async def _send_mafia_chat(self, room: Room, chat: ChatMessage) -> None:
        """Push one new chat message to mafia phones only. Nobody else receives
        anything, not even a view update, so villagers can't tell when the
        mafia are typing (or how much)."""
        event = {"type": "mafia_chat", "message": chat.model_dump(mode="json")}
        sends = [
            _safe_send(conn, event)
            for player_id, conn in room.connections.items()
            if room.state.players[player_id].is_mafia
        ]
        await asyncio.gather(*sends)

    async def _send_error(self, room: Room, player_id: str, code: str, message: str) -> None:
        conn = room.connections.get(player_id)
        if conn is not None:
            await _safe_send(conn, {"type": "error", "code": code, "message": message})

    async def _save(self, room: Room) -> None:
        saved = SavedRoom(state=room.state, tokens=room.tokens, version=room.version)
        await self._store_call(self.store.save(saved), f"save room {room.state.room_code}")

    # ------------------------------------------------------------- helpers

    async def _store_call(self, call: Coroutine[Any, Any, None], what: str) -> None:
        # The store is a backup. If Redis is down, games keep running from
        # memory; we only lose the ability to survive a restart until it's back.
        try:
            await call
        except Exception:
            logger.exception("Could not %s", what)

    def _run_in_background(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _new_room_code(self) -> str:
        while True:
            code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))
            if code not in self.rooms:
                return code

    def _issue_token(self, room: Room, player_id: str) -> str:
        token = secrets.token_urlsafe(24)
        room.tokens[token] = player_id
        self.tokens[token] = (room.state.room_code, player_id)
        return token


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
