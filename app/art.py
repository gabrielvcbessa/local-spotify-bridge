from dataclasses import dataclass
import hashlib
import json
from io import BytesIO
from pathlib import Path
import time

from PIL import Image, ImageEnhance, ImageFilter

from .config import Settings


@dataclass(frozen=True)
class ArtOptions:
    size: int = 180
    theme: str = "dark"
    swap: str = "lvgl"
    variant: str = "player-bg"
    blur: float = 0.0
    darken: float = 0.52
    saturation: float = 0.9
    contrast: float = 1.08
    circle: bool = False

    @property
    def byte_order(self) -> str:
        return "rotary-lvgl" if self.swap == "lvgl" else "big-endian"

    def cache_key(self) -> dict[str, object]:
        return {
            "size": self.size,
            "theme": self.theme,
            "swap": self.swap,
            "variant": self.variant,
            "blur": self.blur,
            "darken": self.darken,
            "saturation": self.saturation,
            "contrast": self.contrast,
            "circle": self.circle,
            "format": "rgb565",
        }


class ArtCache:
    def __init__(self, settings: Settings) -> None:
        self._root = Path(settings.data_path).parent / "art-cache"
        self._max_age_seconds = settings.art_cache_max_age_seconds
        self._max_bytes = settings.art_cache_max_bytes
        self._memory_max_bytes = settings.art_memory_cache_max_bytes
        self._memory_max_age_seconds = settings.art_memory_cache_max_age_seconds
        self._memory: dict[str, tuple[float, bytes]] = {}
        self._memory_bytes = 0

    def path_for(self, image_id: str, options: ArtOptions) -> Path:
        name = (
            f"{image_id}"
            f"-size{options.size}"
            f"-theme{options.theme}"
            f"-swap{options.swap}"
            f"-variant{options.variant}"
            f"-blur{options.blur:g}"
            f"-dark{options.darken:g}"
            f"-sat{options.saturation:g}"
            f"-contrast{options.contrast:g}"
            f"-circle{int(options.circle)}.rgb565"
        )
        return self._root / name

    def memory_key(self, image_id: str, options: ArtOptions) -> str:
        return str(self.path_for(image_id, options).name)

    def get(self, image_id: str, options: ArtOptions) -> bytes | None:
        key = self.memory_key(image_id, options)
        cached_memory = self._memory_get(key)
        if cached_memory is not None:
            return cached_memory

        path = self.path_for(image_id, options)
        if not path.exists():
            return None
        if self._is_expired(path):
            path.unlink(missing_ok=True)
            return None
        path.touch()
        payload = path.read_bytes()
        self._memory_set(key, payload)
        return payload

    def set(self, image_id: str, options: ArtOptions, payload: bytes) -> None:
        self._memory_set(self.memory_key(image_id, options), payload)
        path = self.path_for(image_id, options)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_bytes(payload)
        tmp_path.replace(path)
        self.prune()

    def prune(self) -> None:
        if not self._root.exists():
            return

        now = time.time()
        entries: list[tuple[float, int, Path]] = []
        total_bytes = 0
        for path in self._root.glob("*.rgb565"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue

            if self._max_age_seconds >= 0 and now - stat.st_mtime > self._max_age_seconds:
                path.unlink(missing_ok=True)
                continue

            total_bytes += stat.st_size
            entries.append((stat.st_mtime, stat.st_size, path))

        if self._max_bytes <= 0:
            for _, _, path in entries:
                path.unlink(missing_ok=True)
            return

        for _, size, path in sorted(entries):
            if total_bytes <= self._max_bytes:
                break
            path.unlink(missing_ok=True)
            total_bytes -= size

    def status(self) -> dict[str, object]:
        files = 0
        total_bytes = 0
        oldest_mtime: float | None = None
        newest_mtime: float | None = None
        if self._root.exists():
            for path in self._root.glob("*.rgb565"):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                files += 1
                total_bytes += stat.st_size
                oldest_mtime = stat.st_mtime if oldest_mtime is None else min(oldest_mtime, stat.st_mtime)
                newest_mtime = stat.st_mtime if newest_mtime is None else max(newest_mtime, stat.st_mtime)
        return {
            "path": str(self._root),
            "files": files,
            "bytes": total_bytes,
            "max_bytes": self._max_bytes,
            "max_age_seconds": self._max_age_seconds,
            "memory_files": len(self._memory),
            "memory_bytes": self._memory_bytes,
            "memory_max_bytes": self._memory_max_bytes,
            "memory_max_age_seconds": self._memory_max_age_seconds,
            "oldest_mtime": oldest_mtime,
            "newest_mtime": newest_mtime,
        }

    def _memory_get(self, key: str) -> bytes | None:
        cached = self._memory.get(key)
        if cached is None:
            return None
        cached_at, payload = cached
        if self._memory_max_age_seconds >= 0 and time.time() - cached_at > self._memory_max_age_seconds:
            self._memory.pop(key, None)
            self._memory_bytes -= len(payload)
            self._memory_bytes = max(self._memory_bytes, 0)
            return None
        self._memory[key] = (time.time(), payload)
        return payload

    def _memory_set(self, key: str, payload: bytes) -> None:
        if self._memory_max_bytes <= 0:
            self._memory.clear()
            self._memory_bytes = 0
            return

        existing = self._memory.pop(key, None)
        if existing is not None:
            self._memory_bytes -= len(existing[1])

        if len(payload) > self._memory_max_bytes:
            self._memory_bytes = max(self._memory_bytes, 0)
            self._memory_prune()
            return

        self._memory[key] = (time.time(), payload)
        self._memory_bytes += len(payload)
        self._memory_prune()

    def _memory_prune(self) -> None:
        if self._memory_max_bytes <= 0:
            self._memory.clear()
            self._memory_bytes = 0
            return
        now = time.time()
        if self._memory_max_age_seconds >= 0:
            for key, (cached_at, payload) in list(self._memory.items()):
                if now - cached_at > self._memory_max_age_seconds:
                    self._memory.pop(key, None)
                    self._memory_bytes -= len(payload)
        for key, (_, payload) in sorted(self._memory.items(), key=lambda item: item[1][0]):
            if self._memory_bytes <= self._memory_max_bytes:
                break
            self._memory.pop(key, None)
            self._memory_bytes -= len(payload)
        self._memory_bytes = max(self._memory_bytes, 0)

    def _is_expired(self, path: Path) -> bool:
        if self._max_age_seconds < 0:
            return False
        try:
            return time.time() - path.stat().st_mtime > self._max_age_seconds
        except FileNotFoundError:
            return True


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


def art_version(image_id: str, options: ArtOptions) -> str:
    return stable_hash({"source": image_id, "processing": options.cache_key()})


def bytes_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


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


def color_bar_test_pattern_rgb565(size: int, *, swap: str = "lvgl") -> bytes:
    colors = (
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 255),
        (0, 0, 0),
    )
    image = Image.new("RGB", (size, size))
    pixels = []
    for _y in range(size):
        for x in range(size):
            pixels.append(colors[min(len(colors) - 1, (x * len(colors)) // size)])
    image.putdata(pixels)
    return image_to_rgb565(image, swap=swap)
