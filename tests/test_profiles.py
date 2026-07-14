from app.config import Settings
from app.profiles import bridge_profile_registry


def test_bridge_profile_registry_defaults_to_local_profile():
    registry = bridge_profile_registry(Settings())

    assert registry["schema_version"] == 1
    assert registry["active_profile_id"] == "default"
    assert registry["selection_supported"] is True
    assert registry["selection_transport"] == "bridge_profile_registry"
    assert registry["profiles"] == [
        {
            "id": "default",
            "name": "Local bridge",
            "credential_owner": "local_bridge",
            "token_storage": "bridge_runtime_or_environment",
            "active": True,
        }
    ]


def test_bridge_profile_registry_parses_env_json_and_active_profile():
    registry = bridge_profile_registry(
        Settings(
            BRIDGE_PROFILE_ID="work",
            BRIDGE_PROFILE_REGISTRY_JSON=(
                '[{"id":"home","name":"Home"},'
                '{"id":"work","name":"Work","token_storage":"runtime"}]'
            ),
        )
    )

    assert registry["active_profile_id"] == "work"
    assert registry["profiles"][0]["active"] is False
    assert registry["profiles"][1] == {
        "id": "work",
        "name": "Work",
        "credential_owner": "local_bridge",
        "token_storage": "runtime",
        "active": True,
    }


def test_bridge_profile_registry_falls_back_on_bad_json():
    registry = bridge_profile_registry(
        Settings(BRIDGE_PROFILE_ID="studio", BRIDGE_PROFILE_NAME="Studio", BRIDGE_PROFILE_REGISTRY_JSON="not json")
    )

    assert registry["active_profile_id"] == "studio"
    assert registry["profiles"][0]["id"] == "studio"
    assert registry["profiles"][0]["name"] == "Studio"
