import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "huaji_port", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
package = importlib.util.module_from_spec(spec)
sys.modules["huaji_port"] = package
spec.loader.exec_module(package)
