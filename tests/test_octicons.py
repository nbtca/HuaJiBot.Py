import hashlib
import json

import pytest
from huaji_port.github_bridge import build_post, render_card
from huaji_port.octicons import DIRECTORY, ICONS, icon_mask
from PIL import Image


def body(kind="push"):
    value = {
        "repository": {"full_name": "org/repo", "default_branch": "main"},
        "sender": {"login": "user", "type": "User"},
    }
    if kind == "push":
        value.update(
            ref="refs/heads/main",
            commits=[
                {"id": "abcdef123", "message": "Fix source", "modified": ["main.py"]}
            ],
        )
    else:
        value.update(
            action="created" if kind == "issue_comment" else "opened",
            issue={
                "number": 1,
                "title": "Issue title",
                "body": "body",
                "state": "open",
            },
        )
        if kind == "issue_comment":
            value["comment"] = {"body": "comment"}
    return value


@pytest.mark.parametrize(
    "kind,action,reason,expected,color",
    [
        ("issues", "opened", None, "issue-opened", "#3fb950"),
        ("issues", "reopened", None, "issue-reopened", "#3fb950"),
        ("issues", "closed", "completed", "issue-closed", "#a371f7"),
        ("issues", "closed", "not_planned", "skip", "#8b949e"),
        ("issue_comment", "created", None, "comment", "#8b949e"),
    ],
)
def test_issue_and_comment_state_selects_official_icon(
    kind, action, reason, expected, color
):
    value = body(kind)
    value["action"] = action
    value["issue"]["state_reason"] = reason
    post = build_post(kind, value)
    assert (post.icon, post.color) == (expected, color)


def test_single_commit_multi_commit_push_and_merged_pr_have_distinct_icons():
    value = body()
    assert build_post("push", value).icon == "git-commit"
    value["commits"].append(
        {"id": "1234567", "message": "Another commit", "modified": ["app.py"]}
    )
    assert build_post("push", value).icon == "repo-push"
    value["commits"].append(
        {"id": "merge", "message": "Merge pull request #231 from org/branch\n\nFeature"}
    )
    post = build_post("push", value)
    assert post.icon == "git-merge" and post.color == "#a371f7"


def test_packaged_icons_match_official_source_manifest():
    manifest = json.loads((DIRECTORY / "SOURCE.json").read_text(encoding="utf-8"))
    assert set(manifest["source_paths"]) == ICONS
    assert len(manifest["revision"]) == 40
    for name in ICONS:
        assert (
            hashlib.sha256((DIRECTORY / (name + ".svg")).read_bytes()).hexdigest()
            == manifest["svg_sha256"][name]
        )
        with Image.open(DIRECTORY / (name + ".png")) as image:
            assert image.size == (128, 128) and image.mode == "RGBA"
            mask = image.getchannel("A")
            assert mask.getextrema() == (0, 255) and mask.getbbox()


def test_rendered_header_uses_exact_colored_octicon_mask(tmp_path):
    post = build_post("issues", body("issues"))
    path = tmp_path / "card.png"
    render_card(post, path)
    with Image.open(path) as card:
        observed = card.crop((48, 46, 88, 86))
        expected = Image.new("RGB", (40, 40), "#161b22")
        layer = Image.new("RGBA", (40, 40), post.color)
        layer.putalpha(icon_mask("issue-opened", 40))
        expected.paste(layer, (0, 0), layer)
        assert observed.tobytes() == expected.tobytes()


def test_unknown_icon_cannot_read_arbitrary_file():
    with pytest.raises(ValueError):
        icon_mask("../private", 40)
