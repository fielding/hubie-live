"""Game registry with hot reload: drop a module into this package (or edit one) and call
reload(); new and changed games are picked up between rounds without a restart."""
import importlib
import inspect
import logging
import pkgutil
from pathlib import Path

from .base import Game, IllegalAction  # noqa: F401  (re-exported for plugins and callers)

log = logging.getLogger("games")
PKG = Path(__file__).parent


class GameRegistry:
    def __init__(self):
        self.classes: dict[str, type[Game]] = {}
        self._mtimes: dict[str, float] = {}
        self._modules: dict = {}

    def reload(self) -> list[str]:
        """Import new modules and re-import changed ones. Returns the game ids that changed.
        A module that fails to import is logged and its previous version kept."""
        changed = []
        infos = [i for i in pkgutil.iter_modules([str(PKG)]) if not i.name.startswith("_") and i.name != "base"]
        mtimes = {i.name: (PKG / f"{i.name}.py").stat().st_mtime for i in infos}
        # Reloading a shared module (housebets.py) rebinds its classes, SiteError among them, while the
        # plugins built on it keep the old ones: their `except SiteError` then stops matching what the
        # base raises. So any change reloads every module, in name order (housebets before housebets_*).
        stale = any(self._mtimes.get(n) != m for n, m in mtimes.items())
        for info in infos:
            mtime = mtimes[info.name]
            if not stale:
                continue
            try:
                name = f"{__name__}.{info.name}"
                mod = importlib.reload(self._modules[name]) if name in self._modules else importlib.import_module(name)
                self._modules[name] = mod
                for _, cls in inspect.getmembers(mod, inspect.isclass):
                    if issubclass(cls, Game) and cls is not Game and cls.id:
                        self.classes[cls.id] = cls
                        changed.append(cls.id)
                self._mtimes[info.name] = mtime
            except Exception:
                log.exception("game module %s failed to load; keeping the previous version", info.name)
        return changed

    def create(self, game_id: str, balance: float) -> Game:
        return self.classes[game_id](balance)

    def commands(self) -> dict[str, str]:
        return {cls.command: gid for gid, cls in self.classes.items() if cls.command}
