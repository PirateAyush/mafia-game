"""End-to-end tests through real HTTP requests and a real WebSocket."""

import random

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mafia_game import main
from mafia_game.main import CLOSE_AUTH_TIMEOUT, create_app
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

    with client.websocket_connect(f"/ws/{code}") as ws:
        ws.send_json({"type": "auth", "token": host["token"]})
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


@pytest.mark.parametrize("first_message", ['{"type": "auth", "token": "stolen"}', "hello"])
def test_bad_token_is_closed_with_not_found(client, first_message):
    code = client.post("/rooms", json={"name": "Asha"}).json()["room_code"]
    with client.websocket_connect(f"/ws/{code}") as ws:
        ws.send_text(first_message)
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()
    assert err.value.code == CLOSE_NOT_FOUND


def test_silent_connection_is_closed_after_the_auth_timeout(client, monkeypatch):
    monkeypatch.setattr(main, "AUTH_TIMEOUT_SECONDS", 0.1)  # don't wait 5 real seconds
    code = client.post("/rooms", json={"name": "Asha"}).json()["room_code"]
    with client.websocket_connect(f"/ws/{code}") as ws:
        with pytest.raises(WebSocketDisconnect) as err:
            ws.receive_json()  # we never send the auth message
    assert err.value.code == CLOSE_AUTH_TIMEOUT


def test_health_check(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "rooms": 0}
