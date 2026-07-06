from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from .config import Settings


@dataclass(frozen=True)
class ArtOptions:
    size: int = 180
    theme: str = "dark"
    swap: str = "lvgl"
    blur: float = 0.0
    darken: float = 0.22
    saturation: float = 1.08
    contrast: float = 1.05
    circle: bool = False

    @property
    def byte_order(self) -> str:
        return "lvgl-swap" if self.swap == "lvgl" else "big-endian"


class ArtCache:
    def __init__(self, settings: Settings) -> None:
        self._root = Path(settings.data_path).parent / "art-cache"

    def path_for(self, image_id: str, options: ArtOptions) -> Path:
        name = (
            f"{image_id}"
            f"-size{options.size}"
            f"-theme{options.theme}"
            f"-swap{options.swap}"
            f"-blur{options.blur:g}"
            f"-dark{options.darken:g}"
            f"-sat{options.saturation:g}"
            f"-contrast{options.contrast:g}"
            f"-circle{int(options.circle)}.rgb565"
        )
        return self._root / name

    def get(self, image_id: str, options: ArtOptions) -> bytes | None:
        path = self.path_for(image_id, options)
        if not path.exists():
            return None
        return path.read_bytes()

    def set(self, image_id: str, options: ArtOptions, payload: bytes) -> None:
        path = self.path_for(image_id, options)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_bytes(payload)
        tmp_path.replace(path)


def display_ready_rgb565(original: bytes, options: ArtOptions) -> bytes:
    with Image.open(BytesIO(original)) as image:
        image = image.convert("RGB")
        image = center_crop_square(image)
        image = image.resize((options.size, options.size), Image.Resampling.LANCZOS)

        if options.blur > 0:
            image = image.filter(ImageFilter.GaussianBlur(options.blur))
        if options.saturation != 1:
            image = ImageEnhance.Color(image).enhance(options.saturation)
        if options.contrast != 1:
            image = ImageEnhance.Contrast(image).enhance(options.contrast)
        if options.theme == "dark" and options.darken > 0:
            image = apply_dark_overlay(image, options.darken)
        if options.circle:
            image = apply_circle_mask(image)

        return image_to_rgb565(image, swap=options.swap)


def center_crop_square(image: Image.Image) -> Image.Image:
    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))


def apply_dark_overlay(image: Image.Image, opacity: float) -> Image.Image:
    opacity = max(0.0, min(opacity, 1.0))
    overlay = Image.new("RGB", image.size, (0, 0, 0))
    return Image.blend(image, overlay, opacity)


def apply_circle_mask(image: Image.Image) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    draw = Image.new("L", image.size, 0)
    # Pillow's ImageDraw import is deliberately local to keep the hot path small when unused.
    from PIL import ImageDraw

    ImageDraw.Draw(draw).ellipse((0, 0, image.width - 1, image.height - 1), fill=255)
    mask.paste(draw)
    background = Image.new("RGB", image.size, (0, 0, 0))
    background.paste(image, mask=mask)
    return background


def image_to_rgb565(image: Image.Image, *, swap: str = "none") -> bytes:
    rgb = image.convert("RGB").tobytes()
    pixels = bytearray()
    for offset in range(0, len(rgb), 3):
        red = rgb[offset]
        green = rgb[offset + 1]
        blue = rgb[offset + 2]
        value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        high = (value >> 8) & 0xFF
        low = value & 0xFF
        if swap == "lvgl":
            pixels.append(low)
            pixels.append(high)
        else:
            pixels.append(high)
            pixels.append(low)
    return bytes(pixels)

