"""Game data: what a Mafia game looks like at any moment.

This file holds data only, with no rules and no I/O. We use Pydantic models
instead of plain dataclasses because they give us validation and a free
JSON round-trip, which we'll use later to save games in Redis.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MIN_PLAYERS = 5
MAX_PLAYERS = 12

# Short "read your screen" pauses. Fixed on purpose: they aren't gameplay,
# so the host doesn't need to tune them.
ROLE_REVEAL_SECONDS = 10
DAWN_SECONDS = 6
VERDICT_SECONDS = 6
MAX_CHAT_LENGTH = 200  # characters per mafia chat message
MAX_CHAT_HISTORY = 100  # older messages are dropped


class Phase(StrEnum):
    LOBBY = "LOBBY"
    ROLE_REVEAL = "ROLE_REVEAL"
    NIGHT = "NIGHT"
    DAWN = "DAWN"
    DISCUSSION = "DISCUSSION"
    VOTING = "VOTING"
    VERDICT = "VERDICT"
    GAME_OVER = "GAME_OVER"


class Role(StrEnum):
    MAFIA = "MAFIA"
    DOCTOR = "DOCTOR"
    DETECTIVE = "DETECTIVE"
    VILLAGER = "VILLAGER"


class Team(StrEnum):
    TOWN = "TOWN"
    MAFIA = "MAFIA"


class Settings(BaseModel):
    """What the host can change in the lobby. Each timer has safe limits."""

    # extra="forbid" turns a typo like "night_secs" into an error
    # instead of silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    reveal_role_on_death: bool = False
    night_seconds: int = Field(default=45, ge=15, le=180)
    discussion_seconds: int = Field(default=120, ge=30, le=600)
    voting_seconds: int = Field(default=45, ge=15, le=180)


class Player(BaseModel):
    id: str
    name: str
    role: Role | None = None  # None while in the lobby
    alive: bool = True

    @property
    def is_mafia(self) -> bool:
        return self.role == Role.MAFIA


class ChatMessage(BaseModel):
    id: int  # 1, 2, 3... within one game
    sender_id: str
    text: str
    sent_at: int  # epoch milliseconds


class NightActions(BaseModel):
    """Choices made during the current night. Cleared every night."""

    mafia_votes: dict[str, str] = Field(default_factory=dict)  # mafia id -> target id
    doctor_target: str | None = None
    detective_target: str | None = None
    suspects: dict[str, str] = Field(default_factory=dict)  # villager id -> suspect id


class GameState(BaseModel):
    """The full truth about one room. Only the server ever sees all of it."""

    room_code: str
    host_id: str
    settings: Settings = Field(default_factory=Settings)
    players: dict[str, Player] = Field(default_factory=dict)  # kept in join order

    phase: Phase = Phase.LOBBY
    phase_id: int = 0  # goes up by 1 on every phase change
    phase_ends_at: int | None = None  # epoch milliseconds; None = no timer
    day: int = 0  # 1 during the first night and the day after it

    night: NightActions = Field(default_factory=NightActions)
    votes: dict[str, str | None] = Field(default_factory=dict)  # voter -> target (None = skip)
    last_doctor_target: str | None = None
    detective_results: dict[str, bool] = Field(default_factory=dict)  # target -> is mafia
    mafia_chat: list[ChatMessage] = Field(default_factory=list)  # newest last

    night_victim: str | None = None  # who died last night (shown at dawn)
    eliminated: str | None = None  # who was voted out (shown at the verdict)
    winner: Team | None = None

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]
