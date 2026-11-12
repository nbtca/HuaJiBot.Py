"""Render packaged, official GitHub Octicons using their antialiased alpha masks."""

from functools import lru_cache
from pathlib import Path

from PIL import Image

ICONS = {
    "git-merge",
    "git-pull-request",
    "git-pull-request-closed",
    "git-pull-request-draft",
    "git-commit",
    "repo-push",
    "issue-opened",
    "issue-closed",
    "issue-reopened",
    "skip",
    "comment",
}
DIRECTORY = Path(__file__).resolve().parent / "assets/octicons"


@lru_cache(maxsize=64)
def icon_mask(name, size):
    if name not in ICONS:
        raise ValueError("Unknown GitHub Octicon")
    with Image.open(DIRECTORY / (name + ".png")) as image:
        return image.getchannel("A").resize((size, size), Image.Resampling.LANCZOS)


def draw_icon(image, name, color, position=(48, 46), size=40):
    layer = Image.new("RGBA", (size, size), color)
    layer.putalpha(icon_mask(name, size))
    image.paste(layer, position, layer)
