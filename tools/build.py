"""Build an installable plugin ZIP from an explicit allowlist (never package local config)."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "__init__.py",
    "main.py",
    "routing.py",
    "services.py",
    "state.py",
    "business.py",
    "github_bridge.py",
    "octicons.py",
    "card_assets.py",
    "push_rules.py",
    "qq_delivery.py",
    "views/push-rules/index.html",
    "views/push-rules/app.js",
    "views/push-rules/style.css",
    ".astrbot-plugin/i18n/zh-CN.json",
    "transports.py",
    "runtime.py",
    "summaries.py",
    "configuration.py",
    "ai_port.py",
    "metadata.yaml",
    "requirements.txt",
    "_conf_schema.json",
    "README.md",
    "MIGRATION.md",
    "LICENSE",
    "NOTICE",
    "assets/octicons/LICENSE",
    "assets/octicons/SOURCE.json",
]
for icon in json.loads(
    (ROOT / "assets/octicons/SOURCE.json").read_text(encoding="utf-8")
)["source_paths"]:
    FILES.extend([f"assets/octicons/{icon}.svg", f"assets/octicons/{icon}.png"])


def build():
    target = ROOT / "dist/astrbot_plugin_huaji-v0.6.0.zip"
    target.parent.mkdir(exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, name)
    print(target)
    return target


if __name__ == "__main__":
    build()
