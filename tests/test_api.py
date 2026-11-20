"""End-to-end tests through real HTTP requests and a real WebSocket."""

import random

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mafia_game.main import create_app
from mafia_game.rooms.manager import CLOSE_NOT_FOUND, RoomManager


@pytest.fixture
def client():
    with TestClient(create_app(RoomManager(rng=random.Random(0)))) as test_client:
        yield test_client


def test_create_join_connect_and_start(client):
    host = client.post("/rooms", json={"name": "Asha"}).json()
    code = host["room_code"]
    for name in ["Bo", "Cy", "Di", "Ed"]:
        assert client.post(f"/rooms/{code}/join", json={"name": name}).status_code == 200

    with client.websocket_connect(f"/ws/{code}?token={host['token']}") as ws:
        message = ws.receive_json()
        assert message["type"] == "view"
        assert message["state"]["phase"] == "LOBBY"
        assert len(message["state"]["players"]) == 5

        ws.send_json({"type": "start_game"})
        message = ws.receive_json()
        assert message["state"]["phase"] == "ROLE_REVEAL"
        assert message["state"]["you"]["role"] is not None


def test_join_unknown_room_is_404(client):
    response = client.post("/rooms/NOPE42/join", json={"name": "Bo"})
    assert response.status_code == 404
    assert response.json()["code"] == "ROOM_NOT_FOUND"


def test_duplicate_name_is_400(client):
    code = client.post("/rooms", json={"name": "Asha"}).json()["room_code"]
    response = client.post(f"/rooms/{code}/join", json={"name": "asha"})
    assert response.status_code == 400
    assert response.json()["code"] == "NAME_TAKEN"


def test_bad_token_is_closed_with_not_found(client):
    code = client.post("/rooms", json={"name": "Asha"}).json()["room_code"]
    with client.websocket_connect(f"/ws/{code}?token=stolen") as ws:
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
    assert err.value.code == CLOSE_NOT_FOUND
