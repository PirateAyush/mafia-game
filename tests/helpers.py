"""Shared helpers for building rigged games in tests."""

import random

from mafia_game.engine.models import GameState, Role
from mafia_game.engine.rules import (
    add_player,
    advance_phase,
    create_game,
    start_game,
    submit_night_action,
    submit_vote,
)

NOW = 1_000_000  # a fake "current time" in ms, so tests never depend on the clock

# p0 = mafia, p1 = doctor, p2 = detective, p3 and p4 = villagers
FIVE = [Role.MAFIA, Role.DOCTOR, Role.DETECTIVE, Role.VILLAGER, Role.VILLAGER]

# p0, p1 = mafia, p2 = doctor, p3 = detective, p4 to p7 = villagers
EIGHT = [Role.MAFIA, Role.MAFIA, Role.DOCTOR, Role.DETECTIVE] + [Role.VILLAGER] * 4


def lobby(player_count: int) -> GameState:
    state = create_game("ABCDEF", "p0", "Player0")
    for i in range(1, player_count):
        add_player(state, f"p{i}", f"Player{i}")
    return state


def game_at_night(roles: list[Role]) -> GameState:
    """A game already in night 1, with roles we choose instead of random ones."""
    state = lobby(len(roles))
    start_game(state, "p0", NOW, random.Random(0))
    for i, role in enumerate(roles):
        state.players[f"p{i}"].role = role
    advance_phase(state, NOW)  # ROLE_REVEAL -> NIGHT
    return state


def act(state: GameState, player_id: str, target_id: str) -> None:
    submit_night_action(state, player_id, target_id, state.phase_id)


def vote(state: GameState, voter_id: str, target_id: str | None) -> None:
    submit_vote(state, voter_id, target_id, state.phase_id)


def to_voting(state: GameState) -> None:
    """From DAWN: skip through discussion into voting."""
    advance_phase(state, NOW)  # DAWN -> DISCUSSION
    advance_phase(state, NOW)  # DISCUSSION -> VOTING
