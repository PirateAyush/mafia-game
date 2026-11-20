"""Game rules: every way the game state is allowed to change.

Each public function checks that a move is legal, then changes the state in
place. An illegal move raises GameError with a short code the frontend can
turn into a message.

Nothing here touches the network, Redis or the clock. The current time comes
in as `now_ms` and randomness as `rng`, so tests can control both.
"""

import random
from collections import Counter
from typing import Any

from pydantic import ValidationError

from mafia_game.engine.models import (
    DAWN_SECONDS,
    MAX_PLAYERS,
    MIN_PLAYERS,
    ROLE_REVEAL_SECONDS,
    VERDICT_SECONDS,
    GameState,
    NightActions,
    Phase,
    Player,
    Role,
    Settings,
    Team,
)


class GameError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- helpers


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise GameError(code, message)


def _require_host(state: GameState, player_id: str) -> None:
    _require(player_id == state.host_id, "NOT_HOST", "Only the host can do that")


def _require_phase(state: GameState, phase: Phase) -> None:
    _require(state.phase == phase, "WRONG_PHASE", f"Not allowed during {state.phase}")


def _require_current_phase(state: GameState, phase_id: int) -> None:
    # A tap sent just before the phase changed arrives with the old phase_id.
    _require(phase_id == state.phase_id, "STALE_PHASE", "That phase has already ended")


def _alive_player(state: GameState, player_id: str) -> Player:
    player = state.players.get(player_id)
    _require(player is not None, "UNKNOWN_PLAYER", "Player is not in this room")
    assert player is not None  # tells the type checker what _require guaranteed
    _require(player.alive, "NOT_ALIVE", "Dead players can't act")
    return player


def _alive_target(state: GameState, target_id: str) -> Player:
    target = state.players.get(target_id)
    _require(target is not None and target.alive, "INVALID_TARGET", "Pick a living player")
    assert target is not None
    return target


def _enter_phase(state: GameState, phase: Phase, now_ms: int, seconds: int | None) -> None:
    state.phase = phase
    state.phase_id += 1
    state.phase_ends_at = now_ms + seconds * 1000 if seconds is not None else None


def _clear_round_data(state: GameState) -> None:
    state.day = 0
    state.night = NightActions()
    state.votes = {}
    state.last_doctor_target = None
    state.detective_results = {}
    state.night_victim = None
    state.eliminated = None
    state.winner = None


# ------------------------------------------------------------------ lobby


def create_game(
    room_code: str, host_id: str, host_name: str, settings: Settings | None = None
) -> GameState:
    state = GameState(room_code=room_code, host_id=host_id, settings=settings or Settings())
    add_player(state, host_id, host_name)
    return state


def add_player(state: GameState, player_id: str, name: str) -> None:
    _require_phase(state, Phase.LOBBY)
    name = name.strip()
    _require(1 <= len(name) <= 20, "INVALID_NAME", "Name must be 1 to 20 characters")
    _require(len(state.players) < MAX_PLAYERS, "ROOM_FULL", "This room is full")
    taken = {p.name.casefold() for p in state.players.values()}
    _require(name.casefold() not in taken, "NAME_TAKEN", "Someone already has that name")
    state.players[player_id] = Player(id=player_id, name=name)


def remove_player(state: GameState, player_id: str) -> None:
    """A player leaves the lobby. If it was the host, the next player takes over."""
    _require_phase(state, Phase.LOBBY)
    _require(player_id in state.players, "UNKNOWN_PLAYER", "Player is not in this room")
    del state.players[player_id]
    if player_id == state.host_id and state.players:
        state.host_id = next(iter(state.players))


def transfer_host(state: GameState, new_host_id: str) -> None:
    """Make another player the host. Works in any phase."""
    _require(new_host_id in state.players, "UNKNOWN_PLAYER", "Player is not in this room")
    state.host_id = new_host_id


def update_settings(state: GameState, by: str, changes: dict[str, Any]) -> None:
    _require_host(state, by)
    _require_phase(state, Phase.LOBBY)
    try:
        # Build a whole new Settings so every limit is checked again.
        state.settings = Settings.model_validate({**state.settings.model_dump(), **changes})
    except ValidationError as exc:
        raise GameError("INVALID_SETTINGS", "One or more settings are out of range") from exc


def mafia_count(player_count: int) -> int:
    return max(1, player_count // 4)


def start_game(state: GameState, by: str, now_ms: int, rng: random.Random) -> None:
    _require_host(state, by)
    _require_phase(state, Phase.LOBBY)
    n = len(state.players)
    _require(n >= MIN_PLAYERS, "NOT_ENOUGH_PLAYERS", f"Need at least {MIN_PLAYERS} players")

    roles = [Role.MAFIA] * mafia_count(n) + [Role.DOCTOR, Role.DETECTIVE]
    roles += [Role.VILLAGER] * (n - len(roles))
    rng.shuffle(roles)
    for player, role in zip(state.players.values(), roles, strict=True):
        player.role = role
        player.alive = True

    _clear_round_data(state)
    _enter_phase(state, Phase.ROLE_REVEAL, now_ms, ROLE_REVEAL_SECONDS)


# ------------------------------------------------------------------ night
def submit_night_action(state: GameState, player_id: str, target_id: str, phase_id: int) -> None:
    """Every living player picks someone at night, so nobody stands out by tapping.

    Mafia pick a victim, the doctor saves, the detective checks, and villagers
    name a suspect. A player can change their choice until the night ends.
    """
    _require_phase(state, Phase.NIGHT)
    _require_current_phase(state, phase_id)
    actor = _alive_player(state, player_id)
    target = _alive_target(state, target_id)

    match actor.role:
        case Role.MAFIA:
            _require(not target.is_mafia, "INVALID_TARGET", "Mafia can't target each other")
            state.night.mafia_votes[actor.id] = target.id
        case Role.DOCTOR:
            _require(
                target.id != state.last_doctor_target,
                "INVALID_TARGET",
                "You can't save the same player two nights in a row",
            )
            state.night.doctor_target = target.id
        case Role.DETECTIVE:
            _require(target.id != actor.id, "INVALID_TARGET", "You already know your own role")
            state.night.detective_target = target.id
        case Role.VILLAGER:
            # Only a cover action: it hides who has a real job tonight.
            _require(target.id != actor.id, "INVALID_TARGET", "Pick someone other than yourself")
            state.night.suspects[actor.id] = target.id
        case _:
            raise GameError("NO_NIGHT_ACTION", "You have nothing to do tonight")


def night_choice(state: GameState, player: Player) -> str | None:
    """Who this player picked tonight, or None if they haven't picked yet."""
    night = state.night
    match player.role:
        case Role.MAFIA:
            return night.mafia_votes.get(player.id)
        case Role.DOCTOR:
            return night.doctor_target
        case Role.DETECTIVE:
            return night.detective_target
        case Role.VILLAGER:
            return night.suspects.get(player.id)
        case _:
            return None


def night_actions_complete(state: GameState) -> bool:
    return all(night_choice(state, p) is not None for p in state.alive_players())


def _mafia_choice(mafia_votes: dict[str, str]) -> str | None:
    """The target with the most mafia votes. A tie means nobody is killed."""
    ranked = Counter(mafia_votes.values()).most_common()
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _resolve_night(state: GameState) -> None:
    night = state.night

    victim_id = _mafia_choice(night.mafia_votes)
    if victim_id is not None and victim_id == night.doctor_target:
        victim_id = None  # the doctor saved them
    if victim_id is not None:
        state.players[victim_id].alive = False
    state.night_victim = victim_id

    if night.detective_target is not None:
        is_mafia = state.players[night.detective_target].is_mafia
        state.detective_results[night.detective_target] = is_mafia

    # If the doctor didn't act, next night they may save anyone.
    state.last_doctor_target = night.doctor_target
    state.night = NightActions()
    state.winner = check_winner(state)


# ----------------------------------------------------------------- voting


def submit_vote(state: GameState, voter_id: str, target_id: str | None, phase_id: int) -> None:
    """Vote to eliminate a player, or pass None to skip. Can be changed until voting ends."""
    _require_phase(state, Phase.VOTING)
    _require_current_phase(state, phase_id)
    voter = _alive_player(state, voter_id)
    if target_id is not None:
        _alive_target(state, target_id)
        _require(target_id != voter.id, "INVALID_TARGET", "You can't vote for yourself")
    state.votes[voter.id] = target_id


def voting_complete(state: GameState) -> bool:
    return all(p.id in state.votes for p in state.alive_players())


def _resolve_vote(state: GameState) -> None:
    alive_count = len(state.alive_players())
    counts = Counter(t for t in state.votes.values() if t is not None)

    eliminated = None
    if counts:
        target_id, votes = counts.most_common(1)[0]
        if votes * 2 > alive_count:  # strict majority of living players
            eliminated = target_id
            state.players[target_id].alive = False

    # Votes are public, so we keep them until the next vote for the verdict screen.
    state.eliminated = eliminated
    state.winner = check_winner(state)


def skip_discussion(state: GameState, by: str, now_ms: int) -> None:
    _require_host(state, by)
    _require_phase(state, Phase.DISCUSSION)
    advance_phase(state, now_ms)


# ------------------------------------------------------------ phase flow


def check_winner(state: GameState) -> Team | None:
    alive = state.alive_players()
    mafia = sum(1 for p in alive if p.is_mafia)
    if mafia == 0:
        return Team.TOWN
    if mafia >= len(alive) - mafia:
        return Team.MAFIA
    return None


def should_end_early(state: GameState) -> bool:
    """True once everyone who needs to act this phase has acted."""
    if state.phase == Phase.NIGHT:
        return night_actions_complete(state)
    if state.phase == Phase.VOTING:
        return voting_complete(state)
    return False


def _start_night(state: GameState, now_ms: int) -> None:
    state.day += 1
    state.night = NightActions()
    state.night_victim = None
    state.eliminated = None
    _enter_phase(state, Phase.NIGHT, now_ms, state.settings.night_seconds)


def advance_phase(state: GameState, now_ms: int) -> None:
    """Move to the next phase. Called when a timer runs out or a phase ends early."""
    settings = state.settings
    match state.phase:
        case Phase.ROLE_REVEAL:
            _start_night(state, now_ms)
        case Phase.NIGHT:
            _resolve_night(state)
            _enter_phase(state, Phase.DAWN, now_ms, DAWN_SECONDS)
        case Phase.DAWN:
            if state.winner is not None:
                _enter_phase(state, Phase.GAME_OVER, now_ms, None)
            else:
                _enter_phase(state, Phase.DISCUSSION, now_ms, settings.discussion_seconds)
        case Phase.DISCUSSION:
            state.votes = {}
            _enter_phase(state, Phase.VOTING, now_ms, settings.voting_seconds)
        case Phase.VOTING:
            _resolve_vote(state)
            _enter_phase(state, Phase.VERDICT, now_ms, VERDICT_SECONDS)
        case Phase.VERDICT:
            if state.winner is not None:
                _enter_phase(state, Phase.GAME_OVER, now_ms, None)
            else:
                _start_night(state, now_ms)
        case _:
            raise GameError("WRONG_PHASE", f"{state.phase} has no next phase")


def return_to_lobby(state: GameState, by: str, now_ms: int) -> None:
    """After a game ends, the host brings everyone back to the lobby to play again."""
    _require_host(state, by)
    _require_phase(state, Phase.GAME_OVER)
    for player in state.players.values():
        player.role = None
        player.alive = True
    _clear_round_data(state)
    _enter_phase(state, Phase.LOBBY, now_ms, None)
