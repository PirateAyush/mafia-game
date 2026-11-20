import pytest

from mafia_game.engine.models import MAX_CHAT_HISTORY
from mafia_game.engine.rules import GameError, advance_phase, return_to_lobby, send_mafia_chat
from mafia_game.engine.views import view_for
from tests.helpers import EIGHT, FIVE, NOW, game_at_night, to_voting, vote

# In EIGHT: p0 and p1 are mafia, p2 doctor, p3 detective, p4 to p7 villagers.


def test_mafia_can_chat_at_night():
    state = game_at_night(EIGHT)
    first = send_mafia_chat(state, "p0", "  go for p5  ", NOW)
    second = send_mafia_chat(state, "p1", "agreed", NOW)

    assert (first.id, first.sender_id, first.text) == (1, "p0", "go for p5")
    assert second.id == 2
    assert [m.text for m in state.mafia_chat] == ["go for p5", "agreed"]


@pytest.mark.parametrize(
    ("sender", "text", "code"),
    [
        ("p4", "hello", "NOT_MAFIA"),  # a villager
        ("p0", "   ", "INVALID_MESSAGE"),  # only spaces
        ("p0", "x" * 201, "INVALID_MESSAGE"),  # too long
    ],
)
def test_illegal_chat(sender, text, code):
    state = game_at_night(EIGHT)
    with pytest.raises(GameError) as err:
        send_mafia_chat(state, sender, text, NOW)
    assert err.value.code == code


def test_no_chat_during_the_day():
    state = game_at_night(EIGHT)
    advance_phase(state, NOW)  # NIGHT -> DAWN
    with pytest.raises(GameError) as err:
        send_mafia_chat(state, "p0", "hello", NOW)
    assert err.value.code == "WRONG_PHASE"


def test_dead_mafia_cannot_chat():
    state = game_at_night(EIGHT)
    state.players["p1"].alive = False
    with pytest.raises(GameError) as err:
        send_mafia_chat(state, "p1", "hello", NOW)
    assert err.value.code == "NOT_ALIVE"


def test_only_the_newest_messages_are_kept():
    state = game_at_night(EIGHT)
    for i in range(MAX_CHAT_HISTORY + 5):
        send_mafia_chat(state, "p0", f"message {i}", NOW)

    assert len(state.mafia_chat) == MAX_CHAT_HISTORY
    assert state.mafia_chat[0].text == "message 5"
    assert state.mafia_chat[-1].id == MAX_CHAT_HISTORY + 5  # ids keep counting up


def test_chat_is_cleared_for_a_new_game():
    state = game_at_night(FIVE)
    send_mafia_chat(state, "p0", "hi", NOW)
    advance_phase(state, NOW)
    to_voting(state)
    for voter in ("p1", "p2", "p3"):
        vote(state, voter, "p0")
    advance_phase(state, NOW)  # VERDICT: town wins
    advance_phase(state, NOW)  # GAME_OVER

    return_to_lobby(state, "p0", NOW)
    assert state.mafia_chat == []


def test_only_mafia_ever_see_the_chat():
    state = game_at_night(EIGHT)
    send_mafia_chat(state, "p0", "secret plan", NOW)

    assert [m.text for m in view_for(state, "p1").mafia_chat] == ["secret plan"]
    for player_id in ("p2", "p3", "p4", "p5", "p6", "p7"):
        view = view_for(state, player_id)
        assert view.mafia_chat is None
        assert "secret plan" not in view.model_dump_json()  # the leak test again
