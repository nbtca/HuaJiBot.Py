"""Cached GitHub avatars and color emoji assets for the Pillow card renderer."""

import asyncio
import hashlib
import io
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import emoji
from PIL import Image, ImageDraw, ImageFont

TWEMOJI_VERSION = "17.0.0"


def units(text):
    """Yield complete emoji sequences and individual non-emoji characters."""
    position = 0
    for match in emoji.emoji_list(text):
        yield from text[position : match["match_start"]]
        yield match["emoji"]
        position = match["match_end"]
    yield from text[position:]


def emoji_key(value):
    if "\u200d" not in value:
        value = value.replace("\ufe0f", "")
    return "-".join(f"{ord(character):x}" for character in value)


def image_bytes(raw, size_limit):
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 4_000_000:
            raise ValueError("Card asset dimensions too large")
        image = image.convert("RGBA")
        image.thumbnail((size_limit, size_limit))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


class CardAssets:
    def __init__(self, http, directory, log):
        self.http, self.directory, self.log = http, directory, log
        self.pending = {}
        self.semaphore = asyncio.Semaphore(6)

    async def get(self, key, urls, limit, size):
        if key in self.pending:
            return await self.pending[key]
        task = asyncio.create_task(self.load(key, urls, limit, size))
        self.pending[key] = task
        try:
            return await task
        finally:
            if self.pending.get(key) is task:
                self.pending.pop(key, None)

    async def load(self, key, urls, limit, size):
        path = self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".png")
        cached = None
        try:
            cached = image_bytes(path.read_bytes(), size)
            if time.time() - path.stat().st_mtime < 7 * 86400:
                return cached
        except (OSError, ValueError, Image.DecompressionBombError):
            cached = None
        try:
            async with self.semaphore, asyncio.timeout(5):
                for url in urls:
                    try:
                        async with asyncio.timeout(4):
                            async with self.http.get(
                                url, allow_redirects=False
                            ) as response:
                                if response.status != 200:
                                    continue
                                raw = bytearray()
                                async for chunk in response.content.iter_chunked(65536):
                                    raw.extend(chunk)
                                    if len(raw) > limit:
                                        raise ValueError(
                                            "Card asset response too large"
                                        )
                            result = image_bytes(bytes(raw), size)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_suffix(".tmp")
                        temporary.write_bytes(result)
                        temporary.replace(path)
                        return result
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        self.log("Card asset download unavailable; using cached or local fallback.")
        return cached

    async def avatar(self, url):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "avatars.githubusercontent.com":
            return None
        query = dict(parse_qsl(parts.query))
        query["s"] = "96"
        sized = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
        )
        return await self.get("avatar:" + sized, [sized], 524288, 96)

    async def emojis(self, text):
        values = list(dict.fromkeys(item["emoji"] for item in emoji.emoji_list(text)))
        tasks = {}
        for value in values:
            key = emoji_key(value)
            urls = [
                f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@{TWEMOJI_VERSION}/assets/72x72/{key}.png",
                f"https://raw.githubusercontent.com/jdecked/twemoji/v{TWEMOJI_VERSION}/assets/72x72/{key}.png",
            ]
            tasks[value] = asyncio.create_task(
                self.get("twemoji:" + TWEMOJI_VERSION + ":" + key, urls, 65536, 72)
            )
        if not tasks:
            return {}
        try:
            done, pending = await asyncio.wait(tasks.values(), timeout=5.5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return {
                value: task.result()
                for value, task in tasks.items()
                if task in done
                and not task.cancelled()
                and task.exception() is None
                and task.result()
            }
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)


class CardFont:
    def __init__(self, path, size, assets=None):
        self.base = ImageFont.truetype(path, size)
        self.size = size
        self.assets = assets or {}
        self.native_emoji = None
        self.widths = {}

    def width(self, value):
        if value not in self.widths:
            self.widths[value] = (
                self.size + 2 if emoji.is_emoji(value) else self.base.getlength(value)
            )
        return self.widths[value]

    def getlength(self, text):
        return sum(self.width(value) for value in units(text))

    def draw(self, image, position, text, fill):
        draw = ImageDraw.Draw(image)
        x, y = position
        for value in units(text):
            if not emoji.is_emoji(value):
                draw.text((x, y), value, font=self.base, fill=fill)
                x += self.width(value)
                continue
            raw = self.assets.get(value)
            offset = max(0, self.base.getmetrics()[0] - self.size)
            if raw:
                with Image.open(io.BytesIO(raw)) as asset:
                    rendered = asset.convert("RGBA").resize(
                        (self.size, self.size), Image.Resampling.LANCZOS
                    )
                    image.paste(rendered, (round(x), round(y + offset)), rendered)
            else:
                native = Path("C:/Windows/Fonts/seguiemj.ttf")
                if self.native_emoji is None and native.is_file():
                    self.native_emoji = ImageFont.truetype(str(native), self.size)
                supported = False
                if self.native_emoji and len(value.replace("\ufe0f", "")) == 1:
                    mask = self.native_emoji.getmask(value)
                    missing = self.native_emoji.getmask("\U0010ffff")
                    supported = mask.size != missing.size or bytes(mask) != bytes(
                        missing
                    )
                if supported:
                    draw.text(
                        (x, y), value, font=self.native_emoji, embedded_color=True
                    )
                else:
                    # Preserve meaning without drawing a missing-glyph box offline.
                    name = emoji.demojize(value, language="alias").strip(":")
                    small = ImageFont.truetype(self.base.path, max(8, self.size // 3))
                    draw.text((x, y + offset), name[:4], font=small, fill=fill)
            x += self.size + 2
