from io import BytesIO

from PIL import Image

from app.art import ArtOptions, art_version, bytes_hash, display_ready_rgb565, image_to_rgb565
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
    assert response.headers["X-Image-Byte-Order"] == "lvgl-swap"
    assert response.headers["X-Image-Variant"] == "player-bg"
    assert response.headers["X-Image-Version"] == art_version("image-id", ArtOptions(size=2, swap="lvgl"))
    assert response.headers["X-Image-Hash"] == bytes_hash(b"1234")
    assert response.headers["Cache-Control"] == "public, max-age=86400"
