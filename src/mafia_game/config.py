"""App settings, read from environment variables (or a local .env file).

Every setting can be overridden without touching code, e.g.:
    MAFIA_REDIS_URL=redis://localhost:6379/7 uv run uvicorn mafia_game.main:app
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAFIA_", env_file=".env")

    redis_url: str = "redis://localhost:6379/5"
    room_ttl_seconds: int = 6 * 60 * 60  # Redis forgets a room 6 hours after its last save
    idle_room_seconds: int = 30 * 60  # forget a room once nobody has touched it for 30 minutes
    host_grace_seconds: int = 60  # how long the host can be offline before someone takes over
