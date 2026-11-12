import importlib.util
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def test_archive_includes_license_and_only_declared_source(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "plugin_build", ROOT / "tools/build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in module.FILES:
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    (tmp_path / "private.local.json").write_text('{"secret":"must-not-ship"}')
    monkeypatch.setattr(module, "ROOT", tmp_path)
    archive = module.build()
    with ZipFile(archive) as package:
        assert package.testzip() is None
        assert set(package.namelist()) == set(module.FILES)
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in package.read("LICENSE").decode()
        assert b"nbtca/HuaJiBot.NET" in package.read("NOTICE")
