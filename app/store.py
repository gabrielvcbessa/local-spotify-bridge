import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import Settings


class TargetDevice(BaseModel):
    device_id: str | None = None
    device_name: str | None = None


class RuntimeState(BaseModel):
    spotify_refresh_token: str | None = None
    target_device: TargetDevice | None = None


class RuntimeStore:
    def __init__(self, settings: Settings) -> None:
        self._path = Path(settings.data_path)

    def load(self) -> RuntimeState:
        if not self._path.exists():
            return RuntimeState()
        with self._path.open("r", encoding="utf-8") as file:
            payload: dict[str, Any] = json.load(file)
        return RuntimeState.model_validate(payload)

    def save(self, state: RuntimeState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(state.model_dump(mode="json", exclude_none=True), file, indent=2)
            file.write("\n")
        tmp_path.replace(self._path)

    def get_refresh_token(self) -> str | None:
        return self.load().spotify_refresh_token

    def set_refresh_token(self, refresh_token: str) -> RuntimeState:
        state = self.load()
        state.spotify_refresh_token = refresh_token
        self.save(state)
        return state

    def clear_refresh_token(self) -> RuntimeState:
        state = self.load()
        state.spotify_refresh_token = None
        self.save(state)
        return state

    def get_target_device(self) -> TargetDevice | None:
        return self.load().target_device

    def set_target_device(self, target: TargetDevice | None) -> RuntimeState:
        state = self.load()
        state.target_device = target
        self.save(state)
        return state
