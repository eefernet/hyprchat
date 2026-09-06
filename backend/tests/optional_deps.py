import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path


HAS_AIOSQLITE = importlib.util.find_spec("aiosqlite") is not None
HAS_CHROMADB = importlib.util.find_spec("chromadb") is not None
HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
HAS_PYDANTIC = importlib.util.find_spec("pydantic") is not None


def module_stub(name: str, **attrs):
    """Install a real module stub so importlib.find_spec stays well-defined."""
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def install_aiosqlite_stub():
    if HAS_AIOSQLITE:
        return sys.modules.get("aiosqlite")
    return module_stub("aiosqlite", Connection=object, Row=object)


def install_rag_stub(monkeypatch=None):
    async def _noop_async(*_args, **_kwargs):
        return []

    mod = types.ModuleType("rag")
    mod.__spec__ = importlib.machinery.ModuleSpec("rag", loader=None)
    mod.RESEARCH_TOOLS = set()
    mod.hybrid_query = _noop_async
    mod.query = _noop_async
    mod.query_research = _noop_async
    mod.format_context = lambda *_args, **_kwargs: ""
    mod.ensure_embed_model = _noop_async
    mod.parse_file = lambda *_args, **_kwargs: ""
    mod.index_research = _noop_async
    if monkeypatch is not None:
        monkeypatch.setitem(sys.modules, "rag", mod)
    else:
        sys.modules["rag"] = mod
    return mod


def load_route_module(monkeypatch, name):
    """Load a leaf router without importing unrelated RAG/image integrations."""
    package_name = "_hc_test_routes"
    route_dir = Path(__file__).resolve().parents[1] / "routes"
    package = types.ModuleType(package_name)
    package.__path__ = [str(route_dir)]
    monkeypatch.setitem(sys.modules, package_name, package)
    for leaf in ("context", name):
        full_name = f"{package_name}.{leaf}"
        spec = importlib.util.spec_from_file_location(full_name, route_dir / f"{leaf}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, full_name, module)
        spec.loader.exec_module(module)
    return module
