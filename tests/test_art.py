from PIL import Image

from app.main import image_to_rgb565


def test_image_to_rgb565_outputs_two_bytes_per_pixel():
    image = Image.new("RGB", (2, 1))
    image.putdata([(255, 0, 0), (0, 255, 0)])

    payload = image_to_rgb565(image)

    assert payload == bytes([0xF8, 0x00, 0x07, 0xE0])
