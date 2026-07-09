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
    art_cache_max_age_seconds: int = Field(default=604800, alias="ART_CACHE_MAX_AGE_SECONDS")
    art_cache_max_bytes: int = Field(default=1073741824, alias="ART_CACHE_MAX_BYTES")
    art_memory_cache_max_bytes: int = Field(default=1073741824, alias="ART_MEMORY_CACHE_MAX_BYTES")
    art_memory_cache_max_age_seconds: int = Field(default=86400, alias="ART_MEMORY_CACHE_MAX_AGE_SECONDS")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8090, alias="PORT")
    poll_interval_seconds: float = Field(default=3.0, alias="POLL_INTERVAL_SECONDS")
    spotify_idle_poll_interval_seconds: float = Field(default=300.0, alias="SPOTIFY_IDLE_POLL_INTERVAL_SECONDS")
    spotify_background_poll_interval_seconds: float = Field(default=30.0, alias="SPOTIFY_BACKGROUND_POLL_INTERVAL_SECONDS")
    active_consumer_ttl_seconds: float = Field(default=120.0, alias="ACTIVE_CONSUMER_TTL_SECONDS")
    spotify_rate_limit_window_seconds: float = Field(default=30.0, alias="SPOTIFY_RATE_LIMIT_WINDOW_SECONDS")
    spotify_rate_limit_soft_requests_per_window: int = Field(
        default=20,
        alias="SPOTIFY_RATE_LIMIT_SOFT_REQUESTS_PER_WINDOW",
    )
    spotify_rate_limit_soft_ratio: float = Field(default=0.8, alias="SPOTIFY_RATE_LIMIT_SOFT_RATIO")
    spotify_rate_limit_backoff_multiplier: float = Field(
        default=1.25,
        alias="SPOTIFY_RATE_LIMIT_BACKOFF_MULTIPLIER",
    )
    spotify_rate_limit_max_poll_interval_seconds: float = Field(
        default=60.0,
        alias="SPOTIFY_RATE_LIMIT_MAX_POLL_INTERVAL_SECONDS",
    )
    spotify_rate_limit_retry_after_padding_seconds: float = Field(
        default=0.5,
        alias="SPOTIFY_RATE_LIMIT_RETRY_AFTER_PADDING_SECONDS",
    )
    spotify_preload_next_enabled: bool = Field(default=True, alias="SPOTIFY_PRELOAD_NEXT_ENABLED")
    spotify_playlist_sort: str = Field(default="spotify", alias="SPOTIFY_PLAYLIST_SORT")
    spotify_track_end_refresh_padding_seconds: float = Field(
        default=1.0,
        alias="SPOTIFY_TRACK_END_REFRESH_PADDING_SECONDS",
    )
    command_followup_refresh_delays_seconds: str = Field(
        default="0.5,1.5",
        alias="COMMAND_FOLLOWUP_REFRESH_DELAYS_SECONDS",
    )
    state_change_progress_drift_ms: int = Field(default=5000, alias="STATE_CHANGE_PROGRESS_DRIFT_MS")

    mqtt_enabled: bool = Field(default=False, alias="MQTT_ENABLED")
    mqtt_host: str = Field(default="localhost", alias="MQTT_HOST")
    mqtt_port: int = Field(default=1883, alias="MQTT_PORT")
    mqtt_username: str = Field(default="", alias="MQTT_USERNAME")
    mqtt_password: str = Field(default="", alias="MQTT_PASSWORD")
    mqtt_topic_prefix: str = Field(default="local-spotify-bridge", alias="MQTT_TOPIC_PREFIX")
    mqtt_knob_topic_prefix: str = Field(default="rotary", alias="MQTT_KNOB_TOPIC_PREFIX")
    mqtt_knob_device_id: str = Field(default="knob", alias="MQTT_KNOB_DEVICE_ID")
    mqtt_qos: int = Field(default=1, alias="MQTT_QOS")
    mqtt_knob_art_size: int = Field(default=360, alias="MQTT_KNOB_ART_SIZE")
    mqtt_knob_art_swap: str = Field(default="lvgl", alias="MQTT_KNOB_ART_SWAP")
    mqtt_knob_art_variant: str = Field(default="player-bg", alias="MQTT_KNOB_ART_VARIANT")

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

    @property
    def command_followup_refresh_delays(self) -> tuple[float, ...]:
        delays: list[float] = []
        for raw_delay in self.command_followup_refresh_delays_seconds.split(","):
            raw_delay = raw_delay.strip()
            if not raw_delay:
                continue
            try:
                delay = float(raw_delay)
            except ValueError:
                continue
            if delay > 0:
                delays.append(delay)
        return tuple(delays)


@lru_cache
def get_settings() -> Settings:
    return Settings()
