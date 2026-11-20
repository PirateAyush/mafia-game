"""Tests against a real Redis server.

Locally they use your Redis (database 5) under a unique key prefix and delete
only their own keys afterwards. If Redis isn't running they are skipped,
except in CI, where MAFIA_TEST_REDIS_URL is set and a missing Redis is a failure.
"""

import os
import uuid

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from mafia_game.rooms.store import RedisRoomStore, SavedRoom
from tests.helpers import FIVE, act, game_at_night

pytestmark = pytest.mark.anyio

REDIS_URL = os.environ.get("MAFIA_TEST_REDIS_URL", "redis://localhost:6379/5")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def store():
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except (RedisError, OSError):
        await client.aclose()
        if "MAFIA_TEST_REDIS_URL" in os.environ:
            raise  # in CI, Redis must be there
        pytest.skip("Redis is not running")

    prefix = f"mafia-test:{uuid.uuid4().hex}:"
    yield RedisRoomStore(client, ttl_seconds=60, prefix=prefix)

    async for key in client.scan_iter(match=prefix + "*"):
        await client.delete(key)
    await client.aclose()


def saved_room() -> SavedRoom:
    state = game_at_night(FIVE)
    act(state, "p0", "p3")
    return SavedRoom(state=state, tokens={"secret-token": "p3"}, version=7)


async def test_save_and_load_round_trip(store):
    room = saved_room()
    await store.save(room)

    assert await store.load_all() == [room]


async def test_saved_rooms_expire(store):
    room = saved_room()
    await store.save(room)

    ttl = await store.client.ttl(store.key(room.state.room_code))
    assert 0 < ttl <= 60


async def test_delete_removes_the_room(store):
    room = saved_room()
    await store.save(room)
    await store.delete(room.state.room_code)

    assert await store.load_all() == []


async def test_unreadable_rooms_are_skipped(store):
    await store.client.set(store.prefix + "BROKEN", "not json", ex=60)
    await store.save(saved_room())

    assert len(await store.load_all()) == 1
