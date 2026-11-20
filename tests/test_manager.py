"""Room manager tests, using fake connections and a fake clock instead of real sockets."""

import random

import pytest

from mafia_game.engine.models import Phase, Role, Settings
from mafia_game.engine.rules import GameError
from mafia_game.rooms.manager import CLOSE_REPLACED, RoomManager
from mafia_game.rooms.store import MemoryRoomStore

pytestmark = pytest.mark.anyio  # run every test in this file inside an event loop

ROLES = [Role.MAFIA, Role.DOCTOR, Role.DETECTIVE, Role.VILLAGER, Role.VILLAGER]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now


class FakeConnection:
    """Records everything the server sends, like a phone that never drops."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    def last(self, kind: str) -> dict:
        return next(m for m in reversed(self.sent) if m["type"] == kind)

    def view(self) -> dict:
        return self.last("view")["state"]


@pytest.fixture
async def manager():
    rooms = RoomManager(clock=FakeClock(), rng=random.Random(0))
    yield rooms
    await rooms.close()  # cancel any timers still sleeping


async def room_with_players(rooms: RoomManager, count: int = 5):
    """Returns (room, [player ids], [fake connections]), everyone connected."""
    code, host_id, _ = await rooms.create_room("Player0", Settings())
    ids = [host_id]
    for i in range(1, count):
        player_id, _ = await rooms.join_room(code, f"Player{i}")
        ids.append(player_id)
    room = rooms.get_room(code)
    conns = []
    for player_id in ids:
        conn = FakeConnection()
        await rooms.connect(room, player_id, conn)
        conns.append(conn)
    return room, ids, conns


async def started_at_night(rooms: RoomManager):
    """A connected game in night 1, with p0 mafia, p1 doctor, p2 detective."""
    room, ids, conns = await room_with_players(rooms)
    await rooms.handle(room, ids[0], '{"type": "start_game"}')
    for player_id, role in zip(ids, ROLES, strict=True):
        room.state.players[player_id].role = role
    await rooms._on_timer(room, room.state.phase_id)  # role reveal ends
    return room, ids, conns


async def test_seats_are_found_by_code_and_token(manager):
    code, host_id, token = await manager.create_room("Asha", Settings())

    room, player_id = manager.find_player(code.lower(), token)
    assert player_id == host_id
    assert manager.find_player(code, "wrong-token") is None
    assert manager.find_player("ZZZZZZ", token) is None


async def test_joining_unknown_room_fails(manager):
    with pytest.raises(GameError) as err:
        await manager.join_room("NOPE42", "Bo")
    assert err.value.code == "ROOM_NOT_FOUND"


async def test_connecting_sends_your_view_and_shows_you_online(manager):
    room, ids, conns = await room_with_players(manager)
    view = conns[0].view()
    assert view["phase"] == "LOBBY"
    assert all(p["online"] for p in view["players"])


async def test_new_player_appears_for_everyone_in_the_lobby(manager):
    code, host_id, _ = await manager.create_room("Asha", Settings())
    room = manager.get_room(code)
    host = FakeConnection()
    await manager.connect(room, host_id, host)

    await manager.join_room(code, "Bo")
    assert [p["name"] for p in host.view()["players"]] == ["Asha", "Bo"]


async def test_bad_messages_get_an_error(manager):
    room, ids, conns = await room_with_players(manager)
    for raw in ['{"type": "dance"}', "not json", '{"type": "vote", "phase_id": 1}']:
        await manager.handle(room, ids[0], raw)
        assert conns[0].last("error")["code"] == "BAD_MESSAGE"


async def test_rule_errors_go_only_to_the_sender(manager):
    room, ids, conns = await room_with_players(manager)
    host_messages_before = len(conns[0].sent)

    await manager.handle(room, ids[1], '{"type": "start_game"}')

    assert conns[1].last("error")["code"] == "NOT_HOST"
    assert len(conns[0].sent) == host_messages_before


async def test_start_game_gives_everyone_their_own_role(manager):
    room, ids, conns = await room_with_players(manager)
    await manager.handle(room, ids[0], '{"type": "start_game"}')

    for conn in conns:
        view = conn.view()
        assert view["phase"] == "ROLE_REVEAL"
        assert view["you"]["role"] is not None
    assert room.timer is not None  # the role reveal countdown is running


async def test_timer_moves_to_the_next_phase(manager):
    room, ids, conns = await room_with_players(manager)
    await manager.handle(room, ids[0], '{"type": "start_game"}')

    await manager._on_timer(room, room.state.phase_id)

    assert room.state.phase == Phase.NIGHT
    assert conns[3].view()["phase"] == "NIGHT"


async def test_stale_timer_does_nothing(manager):
    room, ids, conns = await room_with_players(manager)
    await manager.handle(room, ids[0], '{"type": "start_game"}')
    old_phase_id = room.state.phase_id
    await manager._on_timer(room, old_phase_id)  # now in NIGHT

    await manager._on_timer(room, old_phase_id)  # the old timer fires late
    assert room.state.phase == Phase.NIGHT


async def test_night_ends_early_once_everyone_picks(manager):
    room, ids, conns = await started_at_night(manager)
    phase_id = room.state.phase_id
    picks = [(0, 3), (1, 4), (2, 0), (3, 0), (4, 1)]  # (who, target) by seat number
    for who, target in picks:
        raw = f'{{"type": "night_action", "target": "{ids[target]}", "phase_id": {phase_id}}}'
        await manager.handle(room, ids[who], raw)

    assert room.state.phase == Phase.DAWN
    assert conns[4].view()["night_victim"] == ids[3]


async def test_reconnecting_replaces_the_old_socket(manager):
    room, ids, conns = await room_with_players(manager)
    new_conn = FakeConnection()

    await manager.connect(room, ids[1], new_conn)
    assert conns[1].closed_with == CLOSE_REPLACED

    await manager.disconnect(room, ids[1], conns[1])  # the old socket's cleanup runs late
    assert room.connections[ids[1]] is new_conn  # ...and must not kick the new one


async def test_disconnect_shows_player_offline(manager):
    room, ids, conns = await room_with_players(manager)
    await manager.disconnect(room, ids[2], conns[2])

    players = {p["id"]: p for p in conns[0].view()["players"]}
    assert players[ids[2]]["online"] is False
    assert players[ids[1]]["online"] is True


# ---------------------------------------------------- saving and restoring


async def test_restart_brings_rooms_and_seats_back():
    store, clock = MemoryRoomStore(), FakeClock()
    before = RoomManager(clock=clock, rng=random.Random(0), store=store)
    room, ids, conns = await room_with_players(before)
    await before.handle(room, ids[0], '{"type": "start_game"}')
    token = next(t for t, p in room.tokens.items() if p == ids[3])

    after = RoomManager(clock=clock, rng=random.Random(0), store=store)  # the "new server"
    assert await after.restore() == 1

    restored, player_id = after.find_player(room.state.room_code, token)
    assert player_id == ids[3]  # same seat, same token
    assert restored.state == room.state
    assert restored.version == room.version  # views never go backwards for phones
    assert restored.timer is not None  # the role reveal countdown is running again
    await before.close()
    await after.close()


async def test_overdue_phase_moves_on_right_after_restart():
    store, clock = MemoryRoomStore(), FakeClock()
    before = RoomManager(clock=clock, rng=random.Random(0), store=store)
    room, ids, conns = await room_with_players(before)
    await before.handle(room, ids[0], '{"type": "start_game"}')

    clock.now += 11_000  # the server was down past the end of role reveal
    after = RoomManager(clock=clock, rng=random.Random(0), store=store)
    await after.restore()
    restored = after.get_room(room.state.room_code)
    await restored.timer  # fires at once, because its end time has already passed

    assert restored.state.phase == Phase.NIGHT
    await before.close()
    await after.close()


async def test_broken_store_does_not_stop_the_game():
    class BrokenStore(MemoryRoomStore):
        async def save(self, room):
            raise ConnectionError("Redis is down")

    rooms = RoomManager(clock=FakeClock(), rng=random.Random(0), store=BrokenStore())
    room, ids, conns = await room_with_players(rooms)
    await rooms.handle(room, ids[0], '{"type": "start_game"}')

    assert conns[0].view()["phase"] == "ROLE_REVEAL"
    await rooms.close()


# --------------------------------------------------------- host hand-over


async def test_host_is_handed_over_after_the_grace_period(manager):
    room, ids, conns = await room_with_players(manager)
    await manager.disconnect(room, ids[0], conns[0])

    manager.clock.now += 30_000
    await manager._check_host(room)
    assert room.state.host_id == ids[0]  # still within the 60 seconds

    manager.clock.now += 31_000
    await manager._check_host(room)
    assert room.state.host_id == ids[1]  # the next player in join order
    assert conns[1].view()["you"]["is_host"] is True


async def test_host_who_comes_back_in_time_stays_host(manager):
    room, ids, conns = await room_with_players(manager)
    await manager.disconnect(room, ids[0], conns[0])
    manager.clock.now += 30_000
    await manager.connect(room, ids[0], FakeConnection())

    manager.clock.now += 60_000
    await manager._check_host(room)
    assert room.state.host_id == ids[0]


async def test_after_restart_first_player_back_takes_over_from_a_missing_host():
    store, clock = MemoryRoomStore(), FakeClock()
    before = RoomManager(clock=clock, rng=random.Random(0), store=store)
    room, ids, conns = await room_with_players(before)

    after = RoomManager(clock=clock, rng=random.Random(0), store=store)
    await after.restore()
    restored = after.get_room(room.state.room_code)
    clock.now += 61_000
    await after.connect(restored, ids[2], FakeConnection())

    assert restored.state.host_id == ids[2]
    await before.close()
    await after.close()


# ---------------------------------------------------------------- sweeper


async def test_idle_empty_rooms_are_forgotten(manager):
    code, host_id, token = await manager.create_room("Asha", Settings())

    manager.clock.now += 31 * 60 * 1000
    await manager.sweep()

    assert code not in manager.rooms
    assert manager.find_player(code, token) is None
    assert manager.store.saved == {}


async def test_rooms_with_players_online_are_kept(manager):
    room, ids, conns = await room_with_players(manager)

    manager.clock.now += 31 * 60 * 1000
    await manager.sweep()

    assert room.state.room_code in manager.rooms


# ------------------------------------------------- chat and rate limiting


async def test_mafia_chat_reaches_only_mafia_phones(manager):
    room, ids, conns = await started_at_night(manager)  # ids[0] is the only mafia
    sent_before = [len(conn.sent) for conn in conns]

    await manager.handle(room, ids[0], '{"type": "mafia_chat", "text": "Player3 tonight"}')

    assert conns[0].last("mafia_chat")["message"]["text"] == "Player3 tonight"
    for conn, before in zip(conns[1:], sent_before[1:], strict=True):
        assert len(conn.sent) == before  # nothing at all, not even a view update
    assert "Player3 tonight" in manager.store.saved[room.state.room_code]


async def test_flooding_is_rate_limited(manager):
    room, ids, conns = await room_with_players(manager)
    for _ in range(10):  # the burst allowance
        await manager.handle(room, ids[1], '{"type": "start_game"}')
    assert conns[1].last("error")["code"] == "NOT_HOST"  # let through, then refused by the rules

    await manager.handle(room, ids[1], '{"type": "start_game"}')
    assert conns[1].last("error")["code"] == "RATE_LIMITED"  # the 11th is blocked

    manager.clock.now += 1_000  # one second refills two messages
    await manager.handle(room, ids[1], '{"type": "start_game"}')
    assert conns[1].last("error")["code"] == "NOT_HOST"
