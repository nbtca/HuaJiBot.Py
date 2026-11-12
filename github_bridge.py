"""GitHubBridge event selection, push summaries, cards and authenticated short links."""

import asyncio
import hashlib
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import emoji
from PIL import Image, ImageChops, ImageDraw, ImageOps

from .card_assets import CardAssets, CardFont, units
from .octicons import draw_icon

MERGE_COMMIT = re.compile(
    r"^Merge pull request #(?P<number>\d+) from \S+[ \t]*(?:\n[ \t]*\n(?P<title>[^\n]+))?"
)
VERBS = {"opened": "打开了", "closed": "关闭了", "reopened": "重新打开了"}
LANGUAGE_COLORS = {
    "C#": "#178600",
    "Python": "#3572a5",
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "Java": "#b07219",
    "Go": "#00add8",
    "Rust": "#dea584",
    "C++": "#f34b7d",
    "C": "#555555",
}


async def read_limited(response, limit):
    content = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        content.extend(chunk)
        if len(content) > limit:
            raise ValueError("GitHub presentation response too large")
    return bytes(content)


def should_broadcast(kind, body):
    if not isinstance(body, dict):
        raise ValueError("GitHub event body must be an object")
    if kind not in {"push", "issues", "issue_comment"}:
        return False
    repo, sender = body.get("repository"), body.get("sender")
    if not isinstance(repo, dict) or not isinstance(sender, dict):
        raise ValueError("GitHub repository and sender are required")
    if not isinstance(repo.get("full_name"), str) or not isinstance(
        sender.get("login"), str
    ):
        raise ValueError("Invalid GitHub repository or sender")
    if sender.get("type") == "Bot" or sender["login"].endswith("[bot]"):
        return False
    if kind == "push":
        commits = body.get("commits")
        if not isinstance(commits, list):
            raise ValueError("GitHub commits must be a list")
        if (
            not repo.get("default_branch")
            or body.get("ref") != "refs/heads/" + repo["default_branch"]
        ):
            return False
        paths = []
        for commit in commits:
            if not isinstance(commit, dict):
                raise ValueError("Invalid GitHub commit")
            for field in ("added", "modified", "removed"):
                values = commit.get(field, [])
                if not isinstance(values, list) or any(
                    not isinstance(p, str) for p in values
                ):
                    raise ValueError("Invalid GitHub file paths")
                paths.extend(values)
        # Dot-prefixed application directories such as .vitepress contain source.
        # Keep root dotfiles and GitHub automation metadata quiet.
        return any(
            not path.startswith(".")
            or ("/" in path and not path.startswith(".github/"))
            for path in paths
        )
    if not isinstance(body.get("issue"), dict):
        raise ValueError("GitHub issue is required")
    if kind == "issues":
        return body.get("action") in VERBS
    if not isinstance(body.get("comment"), dict):
        raise ValueError("GitHub comment is required")
    return body.get("action") == "created"


def push_summary(commits):
    number, title, subjects = None, None, []
    for commit in commits:
        message = (
            str(commit.get("message") or "").replace("\r\n", "\n").replace("\r", "\n")
        )
        match = MERGE_COMMIT.match(message)
        if match:
            number = int(match["number"])
            title = (match["title"] or "Pull request").strip()
        else:
            subjects.append(
                (str(commit.get("id", ""))[:7], message.split("\n")[0].strip())
            )
    return number, title, subjects[:5], max(0, len(subjects) - 5)


@dataclass(frozen=True)
class GitHubPost:
    kind: str
    repository: str
    title: str
    lines: tuple[str, ...]
    footer: str
    url: str
    stats: str = ""
    color: str = "#58a6ff"
    language: str = ""
    avatar_url: str = ""
    icon: str = ""

    def text(self, link=None):
        parts = [f"[{self.repository}] {self.title}", *self.lines, self.footer]
        if self.url:
            parts.append(link or self.url)
        return "\n".join(parts)


def build_post(kind, body):
    repo, sender = body["repository"], body["sender"]
    actor = sender["login"]
    common = {
        "kind": kind,
        "repository": repo["full_name"],
        "language": str(repo.get("language") or ""),
        "avatar_url": str(sender.get("avatar_url") or ""),
    }
    if kind == "push":
        commits = body["commits"]
        number, pr_title, subjects, hidden = push_summary(commits)
        branch = body["ref"].removeprefix("refs/heads/")
        title = (
            f"合并 #{number} · {pr_title}"
            if number
            else f"向 {branch} 推送了 {len(commits)} 个提交"
        )
        lines = [f"{sha}  {subject}" for sha, subject in subjects]
        if hidden:
            lines.append(f"…还有 {hidden} 个提交")
        counts = [
            sum(len(commit.get(field, [])) for commit in commits)
            for field in ("added", "removed", "modified")
        ]
        license_id = (repo.get("license") or {}).get("spdx_id", "")
        stats = f"+{counts[0]}  -{counts[1]}  ~{counts[2]}    ★ {repo.get('stargazers_count', 0)}    forks {repo.get('forks_count', 0)}    issues {repo.get('open_issues_count', 0)}"
        if license_id:
            stats += "    " + license_id
        if number:
            icon, color = "git-merge", "#a371f7"
        elif len(commits) == 1:
            icon, color = "git-commit", "#8b949e"
        else:
            icon, color = "repo-push", "#58a6ff"
        return GitHubPost(
            **common,
            title=title,
            lines=tuple(lines),
            footer=f"@{actor} {'merged' if number else 'pushed'} · {branch}",
            url=str(body.get("compare") or repo.get("html_url") or ""),
            stats=stats,
            icon=icon,
            color=color,
        )
    issue = body["issue"]
    comment = kind == "issue_comment"
    content = body["comment"] if comment else issue
    verb = "评论了" if comment else VERBS[body["action"]]
    title = f"#{issue.get('number', '?')} {issue.get('title', '')}"
    if comment:
        icon, color = "comment", "#8b949e"
    elif body["action"] == "reopened":
        icon, color = "issue-reopened", "#3fb950"
    elif body["action"] == "closed":
        if issue.get("state_reason") == "not_planned":
            icon, color = "skip", "#8b949e"
        else:
            icon, color = "issue-closed", "#a371f7"
    else:
        icon, color = "issue-opened", "#3fb950"
    return GitHubPost(
        **common,
        title=title,
        lines=(str(content.get("body") or "（无正文）")[:12000],),
        footer=f"@{actor} {verb} #{issue.get('number', '?')}",
        url=str(content.get("html_url") or issue.get("html_url") or ""),
        icon=icon,
        color=color,
    )


class GitHubPresentation:
    def __init__(self, http, config, directory, log):
        self.http, self.config, self.directory, self.log = http, config, directory, log
        self.cache = {}
        self.lock = asyncio.Lock()
        self.assets = CardAssets(http, directory.parent / "github-assets", log)

    async def shorten(self, url):
        cfg = self.config.get("short_link", {})
        if not url or not cfg.get("enabled"):
            return url
        async with self.lock:
            if url in self.cache:
                return self.cache[url]
            headers = {"Authorization": "Bearer " + cfg.get("token", "")}
            if cfg.get("access_client_id"):
                headers.update(
                    {
                        "CF-Access-Client-Id": cfg["access_client_id"],
                        "CF-Access-Client-Secret": cfg.get("access_client_secret", ""),
                    }
                )
            try:
                async with asyncio.timeout(3):
                    async with self.http.post(
                        cfg["api"],
                        json={"url": url},
                        headers=headers,
                        allow_redirects=False,
                    ) as response:
                        if not 200 <= response.status < 300:
                            raise ValueError("Short-link request rejected")
                        raw = await read_limited(response, 65536)
                        link = json.loads(raw).get("url")
                        if (
                            not isinstance(link, str)
                            or urlsplit(link).scheme not in {"http", "https"}
                            or not urlsplit(link).netloc
                        ):
                            raise ValueError("Invalid short-link result")
                if len(self.cache) >= 256:
                    self.cache.clear()
                self.cache[url] = link
                return link
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"GitHub short-link fallback: {type(exc).__name__}")
                return url

    async def card(self, post):
        if not self.config.get("cards_enabled", True):
            return None
        try:
            selected = font_path(self.config.get("font_path", ""))
            fonts = {size: CardFont(selected, size) for size in (22, 26, 30, 36)}
            layout = card_layout(post, fonts)
            visible = "\n".join(
                [
                    layout["repository"],
                    *layout["title"],
                    *layout["body"],
                    layout["footer"],
                    layout["stats"],
                    post.language,
                ]
            )
            avatar, emoji_images = await asyncio.gather(
                self.assets.avatar(post.avatar_url), self.assets.emojis(visible)
            )
            digest = hashlib.sha256(
                b"card-assets-v4" + repr(post).encode() + (avatar or b"")
            )
            for value, data in sorted(emoji_images.items()):
                digest.update(value.encode() + data)
            key = digest.hexdigest()
            path = self.directory / f"{key}.png"
            async with self.lock:
                if not path.exists():
                    await asyncio.to_thread(
                        render_card,
                        post,
                        path,
                        self.config.get("font_path", ""),
                        avatar,
                        emoji_images,
                    )
            return str(path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"GitHub card fallback: {type(exc).__name__}")
            return None

    def prune(self, referenced):
        cutoff = time.time() - 7 * 86400
        for path in self.directory.glob("*.png"):
            if str(path) not in referenced and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def font_path(configured=""):
    choices = [
        configured,
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    return next((path for path in choices if path and Path(path).is_file()), None)


def plain_markdown(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[图片]", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+\[[xX]\]\s*", "☑ ", text)
    text = re.sub(r"(?m)^\s*[-*]\s+\[ \]\s*", "□ ", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "• ", text)
    text = text.replace("```", "").replace("**", "").replace("`", "").expandtabs(4)
    return emoji.emojize(text, language="alias")


def wrap_text(text, font, width, max_lines):
    lines = []
    for paragraph in plain_markdown(text).splitlines() or [""]:
        line = ""
        for character in units(paragraph):
            if line and font.getlength(line + character) > width:
                lines.append(line)
                line = ""
            line += character
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = list(units(lines[-1]))
        while last and font.getlength("".join(last) + "…") > width:
            last.pop()
        lines[-1] = "".join(last) + "…"
    return lines


def card_layout(post, fonts):
    return {
        "repository": wrap_text(post.repository.replace("/", " / "), fonts[36], 910, 1)[
            0
        ],
        "title": wrap_text(post.title, fonts[30], 960, 3),
        "body": wrap_text("\n".join(post.lines), fonts[26], 960, 18),
        "footer": wrap_text(post.footer, fonts[22], 910, 1)[0],
        "stats": wrap_text(post.stats, fonts[22], 970, 1)[0],
    }


def render_card(post, path, configured_font="", avatar=None, emoji_images=None):
    selected_font = font_path(configured_font)
    if selected_font is None:
        raise ValueError("No card font available")
    fonts = {
        size: CardFont(selected_font, size, emoji_images) for size in (22, 26, 30, 36)
    }
    layout = card_layout(post, fonts)
    title, body = layout["title"], layout["body"]
    height = 290 + len(title) * 44 + len(body) * 36
    image = Image.new("RGB", (1080, height), "#0d1117")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (12, 12, 1068, height - 12),
        radius=24,
        fill="#161b22",
        outline="#30363d",
        width=2,
    )
    icon = post.icon or {
        "push": "git-commit",
        "issues": "issue-opened",
        "issue_comment": "comment",
    }.get(post.kind, "git-commit")
    draw_icon(image, icon, post.color)
    fonts[36].draw(image, (110, 42), layout["repository"], "#f0f6fc")
    language = post.language
    if language:
        draw.ellipse((54, 107, 66, 119), fill=LANGUAGE_COLORS.get(language, "#8b949e"))
        fonts[22].draw(image, (78, 96), language, "#8b949e")
    y = 144
    for line in title:
        fonts[30].draw(image, (54, y), line, post.color)
        y += 44
    y += 12
    for line in body:
        fonts[26].draw(image, (54, y), line, "#c9d1d9")
        y += 36
    if post.stats:
        fonts[22].draw(image, (54, height - 108), layout["stats"], "#8b949e")
    draw.line((54, height - 76, 1026, height - 76), fill="#30363d")
    avatar_drawn = False
    if avatar:
        try:
            with Image.open(io.BytesIO(avatar)) as original:
                if original.width * original.height <= 4_000_000:
                    thumb = ImageOps.fit(original.convert("RGBA"), (38, 38))
                    mask = Image.new("L", (152, 152))
                    ImageDraw.Draw(mask).ellipse((0, 0, 151, 151), fill=255)
                    mask = mask.resize((38, 38), Image.Resampling.LANCZOS)
                    mask = ImageChops.multiply(mask, thumb.getchannel("A"))
                    image.paste(thumb, (54, height - 61), mask)
                    avatar_drawn = True
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
    if not avatar_drawn:
        draw.ellipse((54, height - 61, 91, height - 24), fill="#30363d")
    fonts[22].draw(image, (110, height - 60), layout["footer"], "#8b949e")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    image.save(temporary, format="PNG")
    temporary.replace(path)
