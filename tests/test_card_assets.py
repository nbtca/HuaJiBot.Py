import asyncio
import hashlib
import io
import os
import time
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from huaji_port.card_assets import CardAssets, CardFont, emoji_key, units
from huaji_port.github_bridge import (
    GitHubPost,
    GitHubPresentation,
    font_path,
    plain_markdown,
    render_card,
    wrap_text,
)
from PIL import Image


def png(color="blue"):
    output = io.BytesIO()
    Image.new("RGBA", (72, 72), color).save(output, format="PNG")
    return output.getvalue()


def test_windows_font_falls_back_when_cjk_fonts_are_unavailable(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: str(path).replace("\\", "/") == "C:/Windows/Fonts/segoeui.ttf",
    )
    assert font_path() == "C:/Windows/Fonts/segoeui.ttf"


@pytest.mark.parametrize(
    "value,key",
    [
        ("❤️", "2764"),
        ("👍🏽", "1f44d-1f3fd"),
        ("🇨🇳", "1f1e8-1f1f3"),
        ("1️⃣", "31-20e3"),
        ("👩‍⚕️", "1f469-200d-2695-fe0f"),
    ],
)
def test_twemoji_sequence_keys(value, key):
    assert emoji_key(value) == key


def test_wrapping_keeps_tones_flags_and_zwj_sequences_together():
    values = ["👍🏽", "🇨🇳", "👨‍👩‍👧‍👦", "1️⃣"]
    assert list(units("".join(values))) == values
    font = CardFont(font_path(), 26)
    assert wrap_text("".join(values), font, 30, 10) == values
    assert plain_markdown(":smile: :+1:") == "😄 👍"


@pytest.mark.asyncio
async def test_avatar_url_uses_small_size_and_preserves_version(tmp_path):
    assets = CardAssets(None, tmp_path, lambda _: None)
    assets.get = AsyncMock(return_value=png())
    await assets.avatar("https://avatars.githubusercontent.com/u/123?v=4")
    url = assets.get.await_args.args[1][0]
    assert "s=96" in url and "v=4" in url
    assert await assets.avatar("https://untrusted.example/avatar.png") is None
    assert assets.get.await_count == 1


@pytest.mark.asyncio
async def test_slow_avatar_is_cached_and_stale_cache_survives_network_failure(tmp_path):
    calls = 0
    failing = False

    async def handler(request):
        nonlocal calls
        calls += 1
        if failing:
            return web.Response(status=503)
        await asyncio.sleep(2.05)
        return web.Response(body=png(), content_type="image/png")

    app = web.Application()
    app.router.add_get("/avatar", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as http:
            assets = CardAssets(http, tmp_path, lambda _: None)
            args = ("avatar:test", [f"http://127.0.0.1:{port}/avatar"], 524288, 96)
            first = await assets.get(*args)
            assert first and await assets.get(*args) == first and calls == 1
            path = tmp_path / (hashlib.sha256(b"avatar:test").hexdigest() + ".png")
            os.utime(path, (time.time() - 8 * 86400,) * 2)
            failing = True
            assert await assets.get(*args) == first and calls == 2
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_emoji_assets_are_deduplicated_and_include_complete_sequences(tmp_path):
    assets = CardAssets(None, tmp_path, lambda _: None)
    assets.get = AsyncMock(return_value=png("green"))
    result = await assets.emojis("👍🏽 👍🏽 🇨🇳 👨‍👩‍👧‍👦")
    assert set(result) == {"👍🏽", "🇨🇳", "👨‍👩‍👧‍👦"} and assets.get.await_count == 3
    assert all("17.0.0" in call.args[1][0] for call in assets.get.await_args_list)


def test_card_draws_avatar_and_emoji_images(tmp_path):
    post = GitHubPost(
        "issue_comment",
        "nbtca/Roadmap",
        "#103 制作协会看板娘形象",
        ("🎉 👍🏽 🇨🇳 👨‍👩‍👧‍👦",),
        "@developer 评论了 #103",
        "https://example.com",
    )
    path = tmp_path / "card.png"
    assets = {value: png("lime") for value in ["🎉", "👍🏽", "🇨🇳", "👨‍👩‍👧‍👦"]}
    render_card(post, path, avatar=png("blue"), emoji_images=assets)
    with Image.open(path) as image:
        assert image.getpixel((73, image.height - 42)) == (0, 0, 255)
        avatar_region = image.crop((52, image.height - 63, 95, image.height - 20))
        assert tuple(int(post.color[i : i + 2], 16) for i in (1, 3, 5)) not in {
            color for _, color in avatar_region.getcolors(1000000)
        }
        colors = dict((color, count) for count, color in image.getcolors(1000000))
        assert colors.get((0, 255, 0), 0) > 500


@pytest.mark.asyncio
async def test_avatar_change_invalidates_placeholder_card(tmp_path):
    post = GitHubPost(
        "issues", "org/repo", "Issue", ("body",), "@user", "https://example.com"
    )
    presentation = GitHubPresentation(None, {}, tmp_path, lambda _: None)
    presentation.assets.avatar = AsyncMock(side_effect=[None, png("blue")])
    presentation.assets.emojis = AsyncMock(return_value={})
    before = await presentation.card(post)
    after = await presentation.card(post)
    assert before and after and before != after
    with Image.open(after) as image:
        assert image.getpixel((73, image.height - 42)) == (0, 0, 255)
