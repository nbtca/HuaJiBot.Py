import asyncio
import json
import os
import sqlite3
import time
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from huaji_port.configuration import normalize
from huaji_port.github_bridge import (
    GitHubPresentation,
    build_post,
    push_summary,
    render_card,
    should_broadcast,
)
from huaji_port.runtime import Runtime
from huaji_port.services import Services
from huaji_port.state import State
from PIL import Image

ORIGIN = "bot:GroupMessage:test"


def event(kind="push"):
    body = {
        "repository": {
            "full_name": "nbtca/HuaJiBot.Py",
            "default_branch": "main",
            "private": False,
            "language": "Python",
            "stargazers_count": 12,
            "forks_count": 3,
            "open_issues_count": 2,
        },
        "sender": {"login": "developer", "type": "User"},
    }
    if kind == "push":
        body.update(
            ref="refs/heads/main",
            compare="https://github.com/nbtca/HuaJiBot.Py/compare/a...b",
            commits=[
                {
                    "id": "abcdef12345",
                    "message": "修复 GitHub 通知\n\n详细说明",
                    "modified": ["main.py"],
                    "added": [],
                    "removed": [],
                }
            ],
        )
    else:
        body.update(
            action="opened" if kind == "issues" else "created",
            issue={
                "number": 42,
                "title": "恢复中文卡片展示",
                "body": "## 需求\n支持 **中文**、多行和 [链接](https://example.com)\n- [x] 已完成",
                "state": "open",
                "html_url": "https://github.com/org/repo/issues/42",
            },
        )
        if kind == "issue_comment":
            body["comment"] = {
                "body": "这是新评论",
                "html_url": "https://github.com/org/repo/issues/42#issuecomment-1",
            }
    return body


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("push", "ref", "refs/heads/feature"),
        ("push", "ref", "refs/tags/v1"),
        ("issues", "action", "edited"),
        ("issues", "action", "labeled"),
        ("issue_comment", "action", "edited"),
        ("issue_comment", "action", "deleted"),
    ],
)
def test_filter_matches_original_dispatcher(kind, field, value):
    body = event(kind)
    assert should_broadcast(kind, body)
    body[field] = value
    assert not should_broadcast(kind, body)


@pytest.mark.parametrize("kind", ["push", "issues", "issue_comment"])
@pytest.mark.parametrize(
    "sender",
    [
        {"login": "automation", "type": "Bot"},
        {"login": "automation[bot]", "type": "User"},
    ],
)
def test_bots_are_filtered(kind, sender):
    body = event(kind)
    body["sender"] = sender
    assert not should_broadcast(kind, body)


def test_empty_and_dotfiles_only_pushes_are_filtered():
    body = event()
    body["commits"][0]["modified"] = [".github/workflows/ci.yml", ".gitignore"]
    assert not should_broadcast("push", body)
    body["commits"][0]["added"] = ["src/main.py"]
    assert should_broadcast("push", body)
    body["commits"] = []
    assert not should_broadcast("push", body)


@pytest.mark.parametrize(
    "path",
    [
        ".vitepress/theme/EditPage.vue",
        ".vitepress/theme/editor/codemirror.ts",
        ".storybook/main.ts",
        ".well-known/security.txt",
    ],
)
def test_application_source_in_dot_directories_is_broadcast(path):
    body = event()
    body["commits"][0]["modified"] = [path]
    assert should_broadcast("push", body)


def test_documents_pr231_merge_is_not_filtered_as_metadata():
    body = event()
    body["repository"]["full_name"] = "nbtca/documents"
    body["commits"] = [
        {
            "id": "a4335701f6111a98c3d5ededf308bd466c9d2945",
            "message": "Merge pull request #231 from nbtca/codex/fix-markdown-error-messages\n\nfix(editor): show Markdown errors beside affected lines",
            "modified": [
                ".vitepress/theme/EditPage.vue",
                ".vitepress/theme/editor/codemirror.ts",
            ],
            "added": [],
            "removed": [],
        }
    ]
    assert should_broadcast("push", body)
    assert "#231" in build_post("push", body).title


@pytest.mark.parametrize("action", ["opened", "closed", "reopened"])
def test_issue_actions_and_comment_links(action):
    body = event("issues")
    body["action"] = action
    assert should_broadcast("issues", body)
    assert "#42" in build_post("issues", body).text()
    body = event("issue_comment")
    assert build_post("issue_comment", body).url.endswith("#issuecomment-1")


def test_merge_summary_and_commit_limit():
    commits = [
        {
            "id": "merge",
            "message": "Merge pull request #123 from org/feature\n\n恢复 GitHub Bridge",
        }
    ]
    commits += [{"id": str(i) * 12, "message": f"Commit {i}\nbody"} for i in range(8)]
    number, title, subjects, hidden = push_summary(commits)
    assert number == 123 and title == "恢复 GitHub Bridge"
    assert len(subjects) == 5 and hidden == 3 and subjects[0][1] == "Commit 0"


@pytest.mark.parametrize("kind", ["push", "issues", "issue_comment"])
def test_card_renders_offline_with_chinese(tmp_path, kind):
    path = tmp_path / (kind + ".png")
    render_card(build_post(kind, event(kind)), path)
    with Image.open(path) as image:
        assert image.width == 1080 and 300 <= image.height <= 1300
        assert len(image.getcolors(maxcolors=1000000)) > 10


@pytest.mark.asyncio
async def test_authenticated_shortlink_caching_and_redirect_fallback(tmp_path):
    calls = []
    followed = []

    async def shorten(request):
        calls.append(dict(request.headers))
        assert await request.json() == {"url": "https://github.com/org/repo"}
        return web.json_response({"url": "https://short.example/abc"}, status=201)

    async def redirect(request):
        raise web.HTTPFound("/leak")

    async def leak(request):
        followed.append(True)
        return web.json_response({"url": "https://wrong.example"})

    app = web.Application()
    app.router.add_post("/short", shorten)
    app.router.add_post("/redirect", redirect)
    app.router.add_route("*", "/leak", leak)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    cfg = {
        "short_link": {
            "enabled": True,
            "api": f"http://127.0.0.1:{port}/short",
            "token": "test-token",
            "access_client_id": "test-id",
            "access_client_secret": "test-secret",
        }
    }
    try:
        async with aiohttp.ClientSession() as http:
            presentation = GitHubPresentation(http, cfg, tmp_path, lambda _: None)
            for _ in range(2):
                assert (
                    await presentation.shorten("https://github.com/org/repo")
                    == "https://short.example/abc"
                )
            assert len(calls) == 1
            assert calls[0]["Authorization"] == "Bearer test-token"
            assert calls[0]["CF-Access-Client-Id"] == "test-id"
            assert calls[0]["CF-Access-Client-Secret"] == "test-secret"
            cfg["short_link"]["api"] = f"http://127.0.0.1:{port}/redirect"
            assert (
                await presentation.shorten("https://github.com/other/repo")
                == "https://github.com/other/repo"
            )
            assert followed == []
    finally:
        await runner.cleanup()


def config():
    return normalize(
        {
            "allowed_sessions": [ORIGIN],
            "extensions_json": json.dumps(
                {"github": {"enabled": True, "groups": {"default": [ORIGIN]}}}
            ),
        }
    )


@pytest.mark.asyncio
async def test_original_mq_envelope_dedupe_and_media_survive_restart(tmp_path):
    cfg = config()
    media = AsyncMock(return_value=True)
    send = AsyncMock(return_value=True)
    runtime = Runtime(cfg, None, tmp_path, send, None, lambda _: None, send_media=media)
    image = runtime.card_directory / "card.png"
    image.parent.mkdir()
    render_card(build_post("push", event()), image)
    runtime.github_presentation.card = AsyncMock(return_value=str(image))
    envelope = {
        "type": "webhook",
        "data": {
            "headers": {"X-GitHub-Event": ["push"], "X-GitHub-Delivery": ["stable-id"]},
            "body": json.dumps(event()),
        },
    }
    await runtime.github_mq_event(envelope)
    await runtime.github_mq_event(envelope)
    runtime.github_presentation.card.assert_awaited_once()
    assert runtime.store.counts() == {"pending": 1}
    await runtime.close()
    runtime = Runtime(cfg, None, tmp_path, send, None, lambda _: None, send_media=media)
    await runtime.delivery_once()
    media.assert_awaited_once()
    send.assert_not_awaited()
    assert runtime.store.counts() == {"sent": 1}
    await runtime.close()


@pytest.mark.asyncio
async def test_image_failure_falls_back_to_text(tmp_path):
    runtime = Runtime(
        config(),
        None,
        tmp_path,
        AsyncMock(return_value=True),
        None,
        lambda _: None,
        send_media=AsyncMock(side_effect=ConnectionError()),
    )
    image = runtime.card_directory / "test.png"
    image.parent.mkdir()
    render_card(build_post("issues", event("issues")), image)
    runtime.enqueue(
        "github:test", ORIGIN, "fallback text", payload={"image": str(image)}
    )
    await runtime.delivery_once()
    runtime.send.assert_awaited_once_with(ORIGIN, "fallback text")
    assert runtime.store.counts() == {"sent": 1}
    await runtime.close()


@pytest.mark.asyncio
async def test_private_or_filtered_event_never_builds_card_or_shortlink(tmp_path):
    runtime = Runtime(
        config(),
        None,
        tmp_path,
        AsyncMock(),
        None,
        lambda _: None,
        send_media=AsyncMock(),
    )
    runtime.github_presentation.card = AsyncMock()
    runtime.github_presentation.shorten = AsyncMock()
    body = event()
    body["repository"]["private"] = True
    await runtime.github_event("push", body, "private")
    body = event("issues")
    body["action"] = "edited"
    await runtime.github_event("issues", body, "edited")
    runtime.github_presentation.card.assert_not_awaited()
    runtime.github_presentation.shorten.assert_not_awaited()
    assert runtime.store.counts() == {}
    await runtime.close()


def test_old_queue_database_migrates_without_losing_text(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE queue (id TEXT PRIMARY KEY,origin TEXT,text TEXT,status TEXT,attempts INTEGER,due REAL,created REAL)"
    )
    db.execute("INSERT INTO queue VALUES ('old','target','text','pending',0,0,0)")
    db.commit()
    db.close()
    state = State(path)
    assert state.next() == ("old", "target", "text", 0)
    assert state.payload("old") is None
    state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["push", "issues", "issue_comment"])
async def test_image_notification_contains_only_card_and_short_link(tmp_path, kind):
    send, media = AsyncMock(return_value=True), AsyncMock(return_value=True)
    runtime = Runtime(
        config(), None, tmp_path, send, None, lambda _: None, send_media=media
    )
    image = runtime.card_directory / "card.png"
    image.parent.mkdir()
    render_card(build_post(kind, event(kind)), image)
    runtime.github_presentation.card = AsyncMock(return_value=str(image))
    runtime.github_presentation.shorten = AsyncMock(
        return_value="https://short.example/abc"
    )
    await runtime.github_event(kind, event(kind), "concise")
    assert runtime.store.next()[2] == "https://short.example/abc"
    await runtime.delivery_once()
    media.assert_awaited_once_with(ORIGIN, "https://short.example/abc", str(image))
    send.assert_not_awaited()
    assert runtime.store.counts() == {"sent": 1}
    await runtime.close()


@pytest.mark.asyncio
async def test_card_delivery_failure_preserves_short_link(tmp_path):
    send = AsyncMock(return_value=True)
    runtime = Runtime(
        config(),
        None,
        tmp_path,
        send,
        None,
        lambda _: None,
        send_media=AsyncMock(side_effect=ConnectionError()),
    )
    image = runtime.card_directory / "card.png"
    image.parent.mkdir()
    render_card(build_post("push", event()), image)
    runtime.github_presentation.card = AsyncMock(return_value=str(image))
    runtime.github_presentation.shorten = AsyncMock(
        return_value="https://short.example/abc"
    )
    await runtime.github_event("push", event(), "failed-image")
    await runtime.delivery_once()
    send.assert_awaited_once_with(ORIGIN, "https://short.example/abc")
    assert runtime.store.counts() == {"sent": 1}
    await runtime.close()


@pytest.mark.asyncio
async def test_github_websocket_normal_entry_delivers_real_card(tmp_path):
    authenticated = []

    async def handler(request):
        authenticated.append(request.headers.get("Authorization"))
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json(
            {
                "type": "webhook",
                "data": {
                    "headers": {
                        "X-GitHub-Event": ["issues"],
                        "X-GitHub-Delivery": ["network-id"],
                    },
                    "body": event("issues"),
                },
            }
        )
        async for _ in socket:
            pass
        return socket

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    cfg = normalize(
        {
            "allowed_sessions": [ORIGIN],
            "extensions_json": json.dumps(
                {
                    "github": {
                        "enabled": True,
                        "address": f"ws://127.0.0.1:{port}/",
                        "token": "mq-test",
                        "groups": {"default": [ORIGIN]},
                    }
                }
            ),
        }
    )
    services = Services(cfg)
    await services.start()
    send, media = AsyncMock(return_value=True), AsyncMock(return_value=True)
    runtime = Runtime(
        cfg, services, tmp_path, send, None, lambda _: None, send_media=media
    )
    try:
        await runtime.start()
        for _ in range(300):
            if runtime.store.counts().get("pending") or media.await_count:
                break
            await asyncio.sleep(0.01)
        await runtime.delivery_once()
        assert authenticated == ["Bearer mq-test"]
        media.assert_awaited_once()
        with Image.open(media.await_args.args[2]) as card:
            assert card.format == "PNG"
        assert media.await_args.args[1] == event("issues")["issue"]["html_url"]
        send.assert_not_awaited()
    finally:
        await runtime.close()
        await services.close()
        await runner.cleanup()


def test_card_cleanup_preserves_referenced_pending_images(tmp_path):
    state = State(tmp_path / "q.db")
    state.bind(ORIGIN)
    directory = tmp_path / "github-cards"
    directory.mkdir()
    used, unused = directory / "used.png", directory / "unused.png"
    for path in (used, unused):
        path.write_bytes(b"old")
        os.utime(path, (time.time() - 8 * 86400,) * 2)
    state.enqueue_parts("image", ORIGIN, "notice", 0, {"image": str(used)})
    presentation = GitHubPresentation(None, {}, directory, lambda _: None)
    presentation.prune(state.card_paths())
    assert used.exists() and not unused.exists()
    state.close()
