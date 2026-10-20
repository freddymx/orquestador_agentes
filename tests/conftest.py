"""Fixtures compartidas.

Los tests no deben depender de la cache real de models.dev de la maquina: sin esto,
un modelo de mentira como `gpt-6-astra` existe o no segun el dia, y el plan cambia.
"""

import json

import pytest

from orquestador import catalog as catalog_mod


@pytest.fixture(autouse=True)
def isolated_models_cache(tmp_path_factory, monkeypatch):
    missing = tmp_path_factory.mktemp("models-cache") / "missing.json"
    monkeypatch.setenv(catalog_mod.MODELS_CACHE_ENV, str(missing))
    catalog_mod._META_CACHE.clear()
    yield
    catalog_mod._META_CACHE.clear()


@pytest.fixture
def models_cache(tmp_path, monkeypatch):
    """Escribe una cache falsa de models.dev y apunta ORQ_MODELS_CACHE a ella."""

    def write(payload):
        path = tmp_path / "models.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv(catalog_mod.MODELS_CACHE_ENV, str(path))
        catalog_mod._META_CACHE.clear()
        return path

    catalog_mod._META_CACHE.clear()
    yield write
    catalog_mod._META_CACHE.clear()
    monkeypatch.delenv(catalog_mod.MODELS_CACHE_ENV, raising=False)
