from app.config import Settings
from app.store import RuntimeStore, TargetDevice


def test_store_persists_refresh_token_and_target(tmp_path):
    store = RuntimeStore(Settings(DATA_PATH=str(tmp_path / "state.json")))

    store.set_refresh_token("refresh-token")
    store.set_target_device(TargetDevice(device_name="Living Room Speaker"))

    reloaded = RuntimeStore(Settings(DATA_PATH=str(tmp_path / "state.json"))).load()

    assert reloaded.spotify_refresh_token == "refresh-token"
    assert reloaded.target_device is not None
    assert reloaded.target_device.device_name == "Living Room Speaker"
