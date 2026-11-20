"""What each player is allowed to see.

The server holds the full GameState, but a phone only ever receives the view
built here. Every field is copied in on purpose (an allowlist). We never copy
the whole state and delete the secrets (a denylist), because then any secret
field added later would leak by default.

Rule of thumb: a player sees what is public, plus what they already know
from their own role. Nothing else.
"""

from collections.abc import Collection

from pydantic import BaseModel

from mafia_game.engine.models import ChatMessage, GameState, Phase, Player, Role, Settings, Team
from mafia_game.engine.rules import GameError, night_choice


class PlayerView(BaseModel):
    id: str
    name: str
    alive: bool
    is_host: bool
    online: bool  # has a phone connected right now
    role: Role | None  # None unless this viewer is allowed to know it


class YouView(BaseModel):
    id: str
    name: str
    alive: bool
    is_host: bool
    role: Role | None
    night_choice: str | None  # your pick tonight, so a reconnected phone can show it


class GameView(BaseModel):
    room_code: str
    phase: Phase
    phase_id: int
    phase_ends_at: int | None
    day: int
    settings: Settings
    you: YouView
    players: list[PlayerView]

    # Public to everyone
    votes: dict[str, str | None]
    night_victim: str | None
    eliminated: str | None
    winner: Team | None

    # Private: filled only for the role that may know it, None for everyone else
    mafia_votes: dict[str, str] | None  # mafia: what your teammates picked
    mafia_chat: list[ChatMessage] | None  # mafia: the team's chat history
    detective_results: dict[str, bool] | None  # detective: player id -> is mafia
    last_doctor_target: str | None  # doctor: who you can't save tonight


def can_see_role(state: GameState, viewer: Player, target: Player) -> bool:
    if state.phase == Phase.GAME_OVER:
        return True  # everything is revealed at the end
    if target.id == viewer.id:
        return True
    if viewer.is_mafia and target.is_mafia:
        return True  # mafia know each other
    if not target.alive and state.settings.reveal_role_on_death:
        return True
    return False


def view_for(state: GameState, viewer_id: str, online: Collection[str] = ()) -> GameView:
    """Build one player's view. `online` holds the ids of players with a phone connected."""
    viewer = state.players.get(viewer_id)
    if viewer is None:
        raise GameError("UNKNOWN_PLAYER", "Player is not in this room")

    players = [
        PlayerView(
            id=p.id,
            name=p.name,
            alive=p.alive,
            is_host=p.id == state.host_id,
            online=p.id in online,
            role=p.role if can_see_role(state, viewer, p) else None,
        )
        for p in state.players.values()
    ]

    you = YouView(
        id=viewer.id,
        name=viewer.name,
        alive=viewer.alive,
        is_host=viewer.id == state.host_id,
        role=viewer.role,
        night_choice=night_choice(state, viewer) if state.phase == Phase.NIGHT else None,
    )

    return GameView(
        room_code=state.room_code,
        phase=state.phase,
        phase_id=state.phase_id,
        phase_ends_at=state.phase_ends_at,
        day=state.day,
        settings=state.settings,
        you=you,
        players=players,
        votes=dict(state.votes),
        night_victim=state.night_victim,
        eliminated=state.eliminated,
        winner=state.winner if state.phase == Phase.GAME_OVER else None,
        mafia_votes=dict(state.night.mafia_votes) if viewer.role == Role.MAFIA else None,
        mafia_chat=list(state.mafia_chat) if viewer.role == Role.MAFIA else None,
        detective_results=(
            dict(state.detective_results) if viewer.role == Role.DETECTIVE else None
        ),
        last_doctor_target=state.last_doctor_target if viewer.role == Role.DOCTOR else None,
    )
