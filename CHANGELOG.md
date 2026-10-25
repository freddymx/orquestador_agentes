# Changelog

Todos los cambios relevantes de `orq` se documentan aquí. Este archivo sigue el formato
[Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y el proyecto se rige por
[Semantic Versioning](https://semver.org/spec/v2.0.0/es.html).

## [0.1.1] - 2026-10-25

### Corregido
- En la nota que sugiere anadir un proveedor local recién sondeado a `opencode.jsonc`, se
  eliminaba el prefijo del modelo (`lemonade: ...`) que se duplicaba como clave dentro del
  bloque pegable (`lemonade: "lemonade": {`, formato roto que rompila el JSON al pegar). La
  nota ahora apunta al objeto `providers` y sugiere el bloque ya indento y con una sola clave.

## [0.1.0] - 2026-10-25

Primera publicación de `orq`: un orquestador de agentes para OpenCode V2 que adapta un
proyecto y sus agentes a los modelos disponibles (locales y en la nube).

### Añadido
- CLI `orq` (Typer) con los comandos `detect`, `models`, `init`, `generate`, `check`,
  `diff`, `compare` y `agents`.
- Escaneo del proyecto: detecta stacks y comandos reales, lee y preserva `opencode.jsonc`
  (soporta JSONC con comentarios) con escritura atómica, diff y backup.
- Descubrimiento y clasificación de endpoints LLM (llama.cpp, Lemonade, Ollama) que se
  exponen como agentes OpenCode y como proveedores en `opencode.jsonc`.
- Modelo de coste: precifica los modelos desde `models.dev` y reparte un único presupuesto
  de tokens entre los agentes.
- Presets de jerarquía (`balanced`, `lean`, `strict`) con subagentes personalizables.
- `generate --endpoint <nombre>@<url>` para sondear y usar modelos de endpoints no
  declarados en `opencode.jsonc`, con un nombre legible de proveedor (`<nombre>@<url>`) en
  vez del slug numérico del host.
- Contrato de tier en `check`: el agente del rol `local` exige un endpoint local (ERROR si
  apunta a la nube, por privacidad). Un rol cloud que usa un modelo local solo avisa cuando
  hay un proveedor cloud disponible, así el fallback de un entorno local no se marca.

### Seguridad
- Endurecimiento del parsing de YAML/JSON generado (guardia de escalares sin comillas para
  evitar la inyección de tipos) y del manejo de credenciales en el `opencode.jsonc` generado.

### Documentación / Metadatos
- Licencia MIT, política de seguridad (`SECURITY.md`) y metadatos públicos (autores, URLs,
  clasificadores).
- Integración con CI en Python 3.11/3.12/3.13 (`pytest`, `ruff`, `mypy`).

### Proceso de publicación (versionado)

Para liberar una nueva versión:

1. Sube el número de versión en **ambos** sitios y que coincidan:
   `pyproject.toml` y `src/orquestador/__init__.py`.
2. Documenta los cambios en la sección de arriba (fecha + número de versión).
3. Etiqueta el commit que contiene el CHANGELOG + el bump:
   `git tag -a vX.Y.Z -m "orq vX.Y.Z"`.
4. Construye el empaque: `uv build` (genera `dist/` con sdist + wheel).
5. Sube al registro (por ejemplo PyPI): `twine upload dist/*`. Requiere credenciales y
   autorización del propietario del proyecto — no se ejecuta desde el entorno de desarrollo.
