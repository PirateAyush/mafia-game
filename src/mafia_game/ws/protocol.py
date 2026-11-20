"""Messages a phone is allowed to send.

Every incoming message is checked against these models before it reaches the
game. Anything that doesn't match exactly (unknown type, missing field,
extra field, wrong type) is rejected here.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartGame(_Message):
    type: Literal["start_game"]


class UpdateSettings(_Message):
    type: Literal["update_settings"]
    changes: dict[str, Any]


class NightAction(_Message):
    type: Literal["night_action"]
    target: str
    phase_id: int


class CastVote(_Message):
    type: Literal["vote"]
    target: str | None  # required, but null means "skip"
    phase_id: int


class SkipDiscussion(_Message):
    type: Literal["skip_discussion"]


class ReturnToLobby(_Message):
    type: Literal["return_to_lobby"]


class MafiaChat(_Message):
    type: Literal["mafia_chat"]
    # A loose cap that rejects huge payloads early. The game's own limit is stricter.
    text: str = Field(max_length=1000)


# The "type" field tells Pydantic which model to use, so it checks only that one.
ClientMessage = Annotated[
    StartGame
    | UpdateSettings
    | NightAction
    | CastVote
    | SkipDiscussion
    | ReturnToLobby
    | MafiaChat,
    Field(discriminator="type"),
]
_client_message = TypeAdapter(ClientMessage)


def parse_client_message(raw: str) -> ClientMessage:
    """Parse and validate one message. Raises pydantic.ValidationError if it's invalid."""
    return _client_message.validate_json(raw)
