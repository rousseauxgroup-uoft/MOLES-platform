"""Loaded workspace files, shared by every plot window.

A file is loaded once, however many windows show it; loaded data is
read-only, so sharing it cannot let one window affect another. The store
follows files renamed or moved in the workspace, and reloads one whose copy
changed on disk.
"""

from typing import Dict, Iterable, Tuple

from .datasets import load_dataset


class DatasetStore:
    def __init__(self, workspace):
        self.workspace = workspace
        self._loaded: Dict[str, Tuple[Tuple[int, int], object]] = {}

    def get(self, rel):
        """The loaded dataset of a workspace file (``UnsupportedDataError`` if it cannot be plotted)."""
        path = self.workspace.path(rel)
        st = path.stat()
        stamp = (st.st_size, st.st_mtime_ns)
        cached = self._loaded.get(rel)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        dataset = load_dataset(path)
        self._loaded[rel] = (stamp, dataset)
        return dataset

    def moved(self, pairs: Iterable[Tuple[str, str]]):
        """Follow files renamed or moved in the workspace: (old, new) workspace paths."""
        for old, new in pairs:
            if old in self._loaded:
                self._loaded[new] = self._loaded.pop(old)

    def forget(self, rels: Iterable[str]):
        for rel in rels:
            self._loaded.pop(rel, None)
