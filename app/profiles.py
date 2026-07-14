import json
from typing import Any

from .config import Settings


def _clean_profile(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    profile_id = str(raw.get("id") or raw.get("profile_id") or "").strip()
    name = str(raw.get("name") or raw.get("display_name") or "").strip()
    if not profile_id:
        profile_id = f"profile-{index + 1}"
    if not name:
        name = profile_id
    if not profile_id or any(ch.isspace() for ch in profile_id):
        return None
    return {
        "id": profile_id,
        "name": name,
        "credential_owner": str(raw.get("credential_owner") or "local_bridge"),
        "token_storage": str(raw.get("token_storage") or "bridge_runtime_or_environment"),
        "active": bool(raw.get("active", False)),
    }


def bridge_profile_registry(settings: Settings) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    if settings.bridge_profile_registry_json.strip():
        try:
            raw_profiles = json.loads(settings.bridge_profile_registry_json)
        except json.JSONDecodeError:
            raw_profiles = []
        if isinstance(raw_profiles, dict):
            raw_profiles = raw_profiles.get("profiles", [])
        if isinstance(raw_profiles, list):
            for index, raw in enumerate(raw_profiles):
                if isinstance(raw, dict):
                    profile = _clean_profile(raw, index)
                    if profile is not None:
                        profiles.append(profile)

    if not profiles:
        profiles.append(
            {
                "id": settings.bridge_profile_id.strip() or "default",
                "name": settings.bridge_profile_name.strip() or "Local bridge",
                "credential_owner": "local_bridge",
                "token_storage": "bridge_runtime_or_environment",
                "active": True,
            }
        )

    active_profile_id = settings.bridge_profile_id.strip() or profiles[0]["id"]
    if active_profile_id not in {profile["id"] for profile in profiles}:
        active_profile_id = profiles[0]["id"]

    for profile in profiles:
        profile["active"] = profile["id"] == active_profile_id

    return {
        "schema_version": 1,
        "active_profile_id": active_profile_id,
        "profiles": profiles,
        "selection_supported": True,
        "selection_transport": "bridge_profile_registry",
    }
