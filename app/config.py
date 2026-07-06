from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    spotify_client_id: str = Field(default="", alias="SPOTIFY_CLIENT_ID")
    spotify_client_secret: str = Field(default="", alias="SPOTIFY_CLIENT_SECRET")
    spotify_refresh_token: str = Field(default="", alias="SPOTIFY_REFRESH_TOKEN")
    spotify_redirect_uri: str = Field(
        default="http://localhost:8090/v1/auth/callback",
        alias="SPOTIFY_REDIRECT_URI",
    )
    spotify_scopes: str = Field(
        default=(
            "user-read-playback-state user-modify-playback-state "
            "playlist-read-private playlist-read-collaborative user-library-read"
        ),
        alias="SPOTIFY_SCOPES",
    )
    data_path: str = Field(default="data/bridge-state.json", alias="DATA_PATH")
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8090, alias="PORT")
    poll_interval_seconds: float = Field(default=3.0, alias="POLL_INTERVAL_SECONDS")
    state_change_progress_drift_ms: int = Field(default=5000, alias="STATE_CHANGE_PROGRESS_DRIFT_MS")

    mqtt_enabled: bool = Field(default=False, alias="MQTT_ENABLED")
    mqtt_host: str = Field(default="localhost", alias="MQTT_HOST")
    mqtt_port: int = Field(default=1883, alias="MQTT_PORT")
    mqtt_username: str = Field(default="", alias="MQTT_USERNAME")
    mqtt_password: str = Field(default="", alias="MQTT_PASSWORD")
    mqtt_topic_prefix: str = Field(default="local-spotify-bridge", alias="MQTT_TOPIC_PREFIX")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @property
    def spotify_configured(self) -> bool:
        return bool(
            self.spotify_client_id
            and self.spotify_client_secret
            and self.spotify_refresh_token
        )

    @property
    def spotify_auth_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def spotify_scope_list(self) -> list[str]:
        return [scope for scope in self.spotify_scopes.split() if scope]


@lru_cache
def get_settings() -> Settings:
    return Settings()
