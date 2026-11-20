from mafia_game.engine.models import Phase, Role
from mafia_game.engine.rules import advance_phase
from mafia_game.engine.views import view_for
from tests.helpers import EIGHT, FIVE, NOW, act, game_at_night, to_voting, vote


def roles_seen_by(state, viewer_id: str) -> dict[str, Role | None]:
    return {p.id: p.role for p in view_for(state, viewer_id).players}


def test_villager_sees_only_their_own_role():
    state = game_at_night(FIVE)
    seen = roles_seen_by(state, "p3")
    assert seen == {"p0": None, "p1": None, "p2": None, "p3": Role.VILLAGER, "p4": None}


def test_no_villager_view_ever_mentions_mafia():
    """The leak test: search the exact JSON a villager's phone would receive."""
    state = game_at_night(EIGHT)
    act(state, "p0", "p4")
    act(state, "p1", "p4")
    for villager in ("p4", "p5", "p6", "p7"):
        assert "MAFIA" not in view_for(state, villager).model_dump_json()


def test_mafia_see_teammates_and_their_picks():
    state = game_at_night(EIGHT)
    act(state, "p1", "p5")

    view = view_for(state, "p0")
    seen = {p.id: p.role for p in view.players}
    assert seen["p1"] == Role.MAFIA
    assert seen["p2"] is None  # mafia don't learn other roles
    assert view.mafia_votes == {"p1": "p5"}

    assert view_for(state, "p4").mafia_votes is None


def test_detective_sees_results_nobody_else_does():
    state = game_at_night(FIVE)
    act(state, "p2", "p0")
    advance_phase(state, NOW)

    assert view_for(state, "p2").detective_results == {"p0": True}
    assert view_for(state, "p3").detective_results is None
    assert view_for(state, "p0").detective_results is None


def test_doctor_sees_who_they_cannot_save():
    state = game_at_night(FIVE)
    act(state, "p1", "p4")
    advance_phase(state, NOW)

    assert view_for(state, "p1").last_doctor_target == "p4"
    assert view_for(state, "p3").last_doctor_target is None


def test_your_night_pick_is_shown_back_to_you_only():
    state = game_at_night(FIVE)
    act(state, "p3", "p0")

    assert view_for(state, "p3").you.night_choice == "p0"
    assert view_for(state, "p4").you.night_choice is None


def test_dead_roles_hidden_by_default():
    state = game_at_night(FIVE)
    act(state, "p0", "p3")
    advance_phase(state, NOW)

    assert roles_seen_by(state, "p4")["p3"] is None


def test_dead_roles_shown_when_host_enables_it():
    state = game_at_night(FIVE)
    state.settings.reveal_role_on_death = True  # as if the host chose it when creating the room
    act(state, "p0", "p3")
    advance_phase(state, NOW)

    assert roles_seen_by(state, "p4")["p3"] == Role.VILLAGER
    assert roles_seen_by(state, "p4")["p1"] is None  # living roles stay hidden


def test_game_over_reveals_everything():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)
    to_voting(state)
    for voter in ("p1", "p2", "p3"):
        vote(state, voter, "p0")
    advance_phase(state, NOW)  # VERDICT
    assert view_for(state, "p3").winner is None  # winner held back until GAME_OVER

    advance_phase(state, NOW)
    view = view_for(state, "p3")
    assert view.phase == Phase.GAME_OVER
    assert view.winner == "TOWN"
    assert all(p.role is not None for p in view.players)


def test_votes_are_public():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)
    to_voting(state)
    vote(state, "p1", "p4")

    assert view_for(state, "p3").votes == {"p1": "p4"}
