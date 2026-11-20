import random

import pytest

from mafia_game.engine.models import GameState, Phase, Role, Team
from mafia_game.engine.rules import (
    GameError,
    add_player,
    advance_phase,
    mafia_count,
    return_to_lobby,
    should_end_early,
    skip_discussion,
    start_game,
    submit_night_action,
    update_settings,
)
from tests.helpers import EIGHT, FIVE, NOW, act, game_at_night, lobby, to_voting, vote

# ----------------------------------------------------------------- lobby


@pytest.mark.parametrize(("players", "mafia"), [(5, 1), (7, 1), (8, 2), (11, 2), (12, 3)])
def test_mafia_count(players, mafia):
    assert mafia_count(players) == mafia


def test_start_game_deals_every_role():
    state = lobby(8)
    start_game(state, "p0", NOW, random.Random(42))

    roles = [p.role for p in state.players.values()]
    assert roles.count(Role.MAFIA) == 2
    assert roles.count(Role.DOCTOR) == 1
    assert roles.count(Role.DETECTIVE) == 1
    assert roles.count(Role.VILLAGER) == 4
    assert state.phase == Phase.ROLE_REVEAL
    assert state.phase_ends_at == NOW + 10_000


def test_start_needs_five_players():
    with pytest.raises(GameError) as err:
        start_game(lobby(4), "p0", NOW, random.Random(0))
    assert err.value.code == "NOT_ENOUGH_PLAYERS"


def test_only_host_can_start():
    with pytest.raises(GameError) as err:
        start_game(lobby(5), "p1", NOW, random.Random(0))
    assert err.value.code == "NOT_HOST"


def test_names_are_unique_ignoring_case():
    state = lobby(2)
    with pytest.raises(GameError) as err:
        add_player(state, "p9", "  player1 ")
    assert err.value.code == "NAME_TAKEN"


def test_room_is_capped_at_twelve():
    state = lobby(12)
    with pytest.raises(GameError) as err:
        add_player(state, "p99", "Late")
    assert err.value.code == "ROOM_FULL"


def test_settings_are_validated():
    state = lobby(5)
    update_settings(state, "p0", {"night_seconds": 60, "reveal_role_on_death": True})
    assert state.settings.night_seconds == 60
    assert state.settings.reveal_role_on_death is True

    for bad in ({"night_seconds": 5}, {"night_secs": 60}):
        with pytest.raises(GameError) as err:
            update_settings(state, "p0", bad)
        assert err.value.code == "INVALID_SETTINGS"
    assert state.settings.night_seconds == 60  # a failed update changes nothing


# ----------------------------------------------------------------- night


def test_mafia_kill_and_detective_check():
    state = game_at_night(FIVE)
    act(state, "p0", "p3")  # mafia targets a villager
    act(state, "p1", "p4")  # doctor saves someone else
    act(state, "p2", "p0")  # detective checks the mafia

    advance_phase(state, NOW)

    assert state.phase == Phase.DAWN
    assert state.night_victim == "p3"
    assert not state.players["p3"].alive
    assert state.detective_results == {"p0": True}
    assert state.winner is None


def test_doctor_save_blocks_the_kill():
    state = game_at_night(FIVE)
    act(state, "p0", "p3")
    act(state, "p1", "p3")
    advance_phase(state, NOW)

    assert state.night_victim is None
    assert state.players["p3"].alive


def test_doctor_cannot_save_same_player_twice_in_a_row():
    state = game_at_night(FIVE)
    act(state, "p1", "p1")  # self-save is allowed once
    advance_phase(state, NOW)  # NIGHT -> DAWN
    to_voting(state)
    advance_phase(state, NOW)  # VOTING (nobody voted) -> VERDICT
    advance_phase(state, NOW)  # VERDICT -> NIGHT 2

    with pytest.raises(GameError) as err:
        act(state, "p1", "p1")
    assert err.value.code == "INVALID_TARGET"


def test_split_mafia_vote_kills_nobody():
    state = game_at_night(EIGHT)
    act(state, "p0", "p4")
    act(state, "p1", "p5")
    advance_phase(state, NOW)

    assert state.night_victim is None


def test_illegal_night_actions():
    state = game_at_night(FIVE)
    cases = [
        ("p3", "p3", "INVALID_TARGET"),  # villager suspects self
        ("p2", "p2", "INVALID_TARGET"),  # detective checks self
        ("p0", "p9", "INVALID_TARGET"),  # unknown target
    ]
    for actor, target, code in cases:
        with pytest.raises(GameError) as err:
            act(state, actor, target)
        assert err.value.code == code


def test_night_ends_early_only_when_everyone_has_picked():
    state = game_at_night(FIVE)
    act(state, "p0", "p3")
    act(state, "p1", "p4")
    act(state, "p2", "p0")
    act(state, "p3", "p0")
    assert not should_end_early(state)  # p4 hasn't picked yet

    act(state, "p4", "p1")
    assert should_end_early(state)


def test_dead_players_are_not_waited_for():
    state = game_at_night(FIVE)
    act(state, "p0", "p3")
    advance_phase(state, NOW)  # p3 dies
    to_voting(state)
    advance_phase(state, NOW)  # nobody voted
    advance_phase(state, NOW)  # NIGHT 2

    for actor, target in [("p0", "p4"), ("p1", "p2"), ("p2", "p4"), ("p4", "p0")]:
        act(state, actor, target)
    assert should_end_early(state)  # p3 is dead, so all living players are done


def test_villager_suspicions_are_cleared_each_night():
    state = game_at_night(FIVE)
    act(state, "p3", "p0")
    assert state.night.suspects == {"p3": "p0"}
    advance_phase(state, NOW)
    assert state.night.suspects == {}


def test_late_tap_from_old_phase_is_rejected():
    state = game_at_night(FIVE)
    old_phase_id = state.phase_id - 1
    with pytest.raises(GameError) as err:
        submit_night_action(state, "p0", "p3", old_phase_id)
    assert err.value.code == "STALE_PHASE"


# ---------------------------------------------------------------- voting


def test_majority_vote_eliminates():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)  # quiet night
    to_voting(state)
    for voter in ("p1", "p2", "p3"):
        vote(state, voter, "p4")  # 3 of 5 is a majority
    advance_phase(state, NOW)

    assert state.phase == Phase.VERDICT
    assert state.eliminated == "p4"
    assert not state.players["p4"].alive


def test_no_majority_means_no_elimination():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)
    to_voting(state)
    vote(state, "p1", "p4")
    vote(state, "p2", "p4")  # 2 of 5 is not a majority
    vote(state, "p3", None)  # skip
    advance_phase(state, NOW)

    assert state.eliminated is None


def test_cannot_vote_for_yourself():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)
    to_voting(state)
    with pytest.raises(GameError) as err:
        vote(state, "p1", "p1")
    assert err.value.code == "INVALID_TARGET"


def test_host_can_skip_discussion():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)  # NIGHT -> DAWN
    advance_phase(state, NOW)  # DAWN -> DISCUSSION
    skip_discussion(state, "p0", NOW)
    assert state.phase == Phase.VOTING


# ----------------------------------------------------------- full games


def test_town_wins_by_voting_out_the_mafia():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)
    to_voting(state)
    for voter in ("p1", "p2", "p3"):
        vote(state, voter, "p0")
    advance_phase(state, NOW)  # VOTING -> VERDICT
    assert state.winner == Team.TOWN

    advance_phase(state, NOW)  # VERDICT -> GAME_OVER
    assert state.phase == Phase.GAME_OVER
    assert state.phase_ends_at is None


def test_mafia_wins_when_equal_to_town():
    state = game_at_night(FIVE)
    act(state, "p0", "p3")
    advance_phase(state, NOW)  # p3 dies: 1 mafia vs 3 town
    to_voting(state)
    for voter in ("p0", "p1", "p2"):
        vote(state, voter, "p4")
    advance_phase(state, NOW)  # p4 voted out: 1 mafia vs 2 town
    assert state.winner is None
    advance_phase(state, NOW)  # VERDICT -> NIGHT 2

    act(state, "p0", "p1")
    advance_phase(state, NOW)  # p1 dies: 1 mafia vs 1 town
    assert state.winner == Team.MAFIA
    advance_phase(state, NOW)
    assert state.phase == Phase.GAME_OVER


def test_play_again_resets_the_room():
    state = game_at_night(FIVE)
    advance_phase(state, NOW)
    to_voting(state)
    for voter in ("p1", "p2", "p3"):
        vote(state, voter, "p0")
    advance_phase(state, NOW)
    advance_phase(state, NOW)  # GAME_OVER

    return_to_lobby(state, "p0", NOW)

    assert state.phase == Phase.LOBBY
    assert all(p.alive and p.role is None for p in state.players.values())
    assert state.winner is None


def test_state_survives_a_json_round_trip():
    """This is how we'll save games to Redis, so it must not lose anything."""
    state = game_at_night(FIVE)
    act(state, "p0", "p3")
    restored = GameState.model_validate_json(state.model_dump_json())
    assert restored == state
