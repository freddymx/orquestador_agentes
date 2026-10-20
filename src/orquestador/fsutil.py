"""Escritura segura de ficheros generados.

Escribir directamente sobre un fichero de configuracion o un agente deja el proyecto
a medias si el proceso muere a mitad. Todo lo que genera `orq` pasa por aqui: se
escribe en un temporal del mismo directorio y se renombra de forma atomica.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Escribe `text` en `path` de forma atomica, conservando permisos."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def backup_file(path: Path) -> Path | None:
    """Copia `path` a `<nombre>.bak` antes de sobrescribirlo. None si no existe."""
    if not path.exists():
        return None
    target = path.with_name(path.name + ".bak")
    shutil.copy2(path, target)
    return target
