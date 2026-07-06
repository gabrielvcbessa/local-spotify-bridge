from io import BytesIO
import os
import time

from PIL import Image

from app.art import ArtCache, ArtOptions, art_version, bytes_hash, color_bar_test_pattern_rgb565, display_ready_rgb565, image_to_rgb565
from app.config import Settings
from app.main import rgb565_response


def test_image_to_rgb565_outputs_big_endian_by_default():
    image = Image.new("RGB", (2, 1))
    image.putdata([(255, 0, 0), (0, 255, 0)])

    payload = image_to_rgb565(image)

    assert payload == bytes([0xF8, 0x00, 0x07, 0xE0])


def test_image_to_rgb565_outputs_lvgl_swapped_bytes():
    image = Image.new("RGB", (2, 1))
    image.putdata([(255, 0, 0), (0, 255, 0)])

    payload = image_to_rgb565(image, swap="lvgl")

    assert payload == bytes([0x00, 0xF8, 0xE0, 0x07])


def test_display_ready_rgb565_returns_exact_knob_payload_size():
    original = Image.new("RGB", (400, 300), (255, 0, 0))
    buffer = BytesIO()
    original.save(buffer, format="JPEG")

    payload = display_ready_rgb565(buffer.getvalue(), ArtOptions(size=180, swap="lvgl"))

    assert len(payload) == 180 * 180 * 2


def test_rgb565_response_sets_knob_contract_headers():
    response = rgb565_response(b"1234", ArtOptions(size=2, swap="lvgl"), "image-id")

    assert response.media_type == "application/octet-stream"
    assert response.headers["X-Image-Width"] == "2"
    assert response.headers["X-Image-Height"] == "2"
    assert response.headers["X-Image-Format"] == "rgb565"
    assert response.headers["X-Image-Byte-Order"] == "rotary-lvgl"
    assert response.headers["X-Image-Target"] == "rotary-os-lvgl-image-source"
    assert response.headers["X-Image-Variant"] == "player-bg"
    assert response.headers["X-Image-Version"] == art_version("image-id", ArtOptions(size=2, swap="lvgl"))
    assert response.headers["X-Image-Hash"] == bytes_hash(b"1234")
    assert response.headers["Cache-Control"] == "public, max-age=86400"


def test_test_pattern_rgb565_uses_obvious_color_bars():
    payload = color_bar_test_pattern_rgb565(180)

    assert len(payload) == 180 * 180 * 2
    assert payload[0:2] == bytes.fromhex("00f8")
    assert payload[36 * 2:36 * 2 + 2] == bytes.fromhex("e007")
    assert payload[72 * 2:72 * 2 + 2] == bytes.fromhex("1f00")
    assert payload[108 * 2:108 * 2 + 2] == bytes.fromhex("ffff")
    assert payload[144 * 2:144 * 2 + 2] == bytes.fromhex("0000")


def test_art_cache_expires_files_older_than_configured_age(tmp_path):
    cache = ArtCache(
        Settings(
            DATA_PATH=str(tmp_path / "bridge-state.json"),
            ART_CACHE_MAX_AGE_SECONDS=1,
            ART_CACHE_MAX_BYTES=1024,
            ART_MEMORY_CACHE_MAX_BYTES=0,
        )
    )
    options = ArtOptions(size=2)
    cache.set("image-old", options, b"1234")
    path = cache.path_for("image-old", options)
    old_time = time.time() - 10
    os.utime(path, (old_time, old_time))

    assert cache.get("image-old", options) is None
    assert not path.exists()


def test_art_cache_serves_hot_payload_from_memory_when_disk_file_is_removed(tmp_path):
    cache = ArtCache(
        Settings(
            DATA_PATH=str(tmp_path / "bridge-state.json"),
            ART_CACHE_MAX_AGE_SECONDS=604800,
            ART_CACHE_MAX_BYTES=1024,
            ART_MEMORY_CACHE_MAX_BYTES=1024,
            ART_MEMORY_CACHE_MAX_AGE_SECONDS=86400,
        )
    )
    options = ArtOptions(size=2)

    cache.set("image-hot", options, b"1234")
    cache.path_for("image-hot", options).unlink()

    assert cache.get("image-hot", options) == b"1234"
    assert cache.status()["memory_files"] == 1
    assert cache.status()["memory_bytes"] == 4


def test_art_memory_cache_expires_entries_older_than_configured_age(tmp_path):
    cache = ArtCache(
        Settings(
            DATA_PATH=str(tmp_path / "bridge-state.json"),
            ART_CACHE_MAX_AGE_SECONDS=604800,
            ART_CACHE_MAX_BYTES=1024,
            ART_MEMORY_CACHE_MAX_BYTES=1024,
            ART_MEMORY_CACHE_MAX_AGE_SECONDS=1,
        )
    )
    options = ArtOptions(size=2)
    cache.set("image-hot", options, b"1234")
    cache.path_for("image-hot", options).unlink()
    key = cache.memory_key("image-hot", options)
    cache._memory[key] = (time.time() - 10, b"1234")

    assert cache.get("image-hot", options) is None
    assert cache.status()["memory_files"] == 0
    assert cache.status()["memory_bytes"] == 0


def test_art_memory_cache_prunes_oldest_payloads_when_size_limit_is_exceeded(tmp_path):
    cache = ArtCache(
        Settings(
            DATA_PATH=str(tmp_path / "bridge-state.json"),
            ART_CACHE_MAX_AGE_SECONDS=604800,
            ART_CACHE_MAX_BYTES=1024,
            ART_MEMORY_CACHE_MAX_BYTES=8,
            ART_MEMORY_CACHE_MAX_AGE_SECONDS=86400,
        )
    )
    options = ArtOptions(size=2)

    cache.set("image-1", options, b"1234")
    cache.set("image-2", options, b"5678")
    cache.set("image-3", options, b"abcd")

    status = cache.status()
    assert status["memory_files"] == 2
    assert status["memory_bytes"] == 8


def test_art_cache_prunes_oldest_files_when_size_limit_is_exceeded(tmp_path):
    cache = ArtCache(
        Settings(
            DATA_PATH=str(tmp_path / "bridge-state.json"),
            ART_CACHE_MAX_AGE_SECONDS=604800,
            ART_CACHE_MAX_BYTES=8,
            ART_MEMORY_CACHE_MAX_BYTES=0,
        )
    )
    options = ArtOptions(size=2)

    cache.set("image-1", options, b"1234")
    first_path = cache.path_for("image-1", options)
    old_time = time.time() - 10
    os.utime(first_path, (old_time, old_time))
    cache.set("image-2", options, b"5678")
    cache.set("image-3", options, b"abcd")

    assert cache.get("image-1", options) is None
    assert cache.get("image-2", options) == b"5678"
    assert cache.get("image-3", options) == b"abcd"


def test_display_ready_rgb565_supports_240_payload_size():
    original = Image.new("RGB", (500, 300), (0, 128, 255))
    buffer = BytesIO()
    original.save(buffer, format="JPEG")

    payload = display_ready_rgb565(buffer.getvalue(), ArtOptions(size=240, swap="lvgl"))

    assert len(payload) == 240 * 240 * 2
