"""Build an installable plugin ZIP from an explicit allowlist (never package local config)."""

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
]


def build():
    target = ROOT / "dist/astrbot_plugin_huaji-v0.2.0.zip"
    target.parent.mkdir(exist_ok=True)
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, name)
    print(target)
    return target


if __name__ == "__main__":
    build()
