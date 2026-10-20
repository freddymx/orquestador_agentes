"""Descubrimiento de modelos disponibles para OpenCode.

Combina cuatro fuentes:
  1. La configuracion de OpenCode (~/.config/opencode/opencode.json[c] y por proyecto).
  2. Sondeo HTTP de los endpoints OpenAI-compatible declarados (llama.cpp, Lemonade, Ollama...).
  3. El catalogo de models.dev que OpenCode cachea en ~/.cache/opencode/models.json
     (tool calling, ventana de contexto y variantes reales de cada modelo).
  4. Un catalogo embebido con precios y capacidades conocidas (para clasificar tiers).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config de OpenCode
# --------------------------------------------------------------------------- #

GLOBAL_CONFIG_DIR = Path.home() / ".config" / "opencode"
CONFIG_NAMES = ("opencode.jsonc", "opencode.json")


def strip_jsonc(text: str) -> str:
    """Elimina comentarios y trailing commas de un JSONC."""
    out, i, n = [], 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    cleaned = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", cleaned)


def load_config_file(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return json.loads(strip_jsonc(raw)) or {}
    except json.JSONDecodeError:
        return {}


def config_paths(project_dir: Path, global_dir: Path | None = None) -> list[Path]:
    """Ordenados de menor a menor prioridad: el global primero, el del proyecto al final."""
    gdir = global_dir or GLOBAL_CONFIG_DIR
    paths: list[Path] = []
    for name in CONFIG_NAMES:
        if (gdir / name).is_file():
            paths.append(gdir / name)
    for name in CONFIG_NAMES:
        if (project_dir / name).is_file():
            paths.append(project_dir / name)
    return paths


def merge_configs(project_dir: Path, global_dir: Path | None = None) -> tuple[dict, list[Path]]:
    """Fusiona config por proyecto y global (gana el proyecto)."""
    merged: dict = {}
    used: list[Path] = []
    for path in config_paths(project_dir, global_dir):
        data = load_config_file(path)
        if not data:
            continue
        used.append(path)
        merged = _deep_merge(merged, data)
    return merged, used


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Proveedores y modelos
# --------------------------------------------------------------------------- #


@dataclass
class Provider:
    name: str
    display_name: str = ""
    base_url: str | None = None
    api_key_present: bool = False
    declared_models: dict[str, dict] = field(default_factory=dict)
    source_config: Path | None = None

    @property
    def is_local(self) -> bool:
        if not self.base_url:
            return False
        url = self.base_url.lower()
        return any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "192.168.", "10.", "172.1", "host.docker.internal"))


@dataclass
class ModelInfo:
    provider: str
    model_id: str
    display_name: str = ""
    source: str = "config"  # config | probe | catalog
    context: int | None = None
    capabilities: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.model_id}"

    @property
    def supports_tools(self) -> bool:
        if self.capabilities:
            return bool(self.capabilities.get("tools", True))
        support = tool_support(self)
        return True if support is None else support

    @property
    def params_b(self) -> float | None:
        """Parametros en miles de millones, deducidos del nombre."""
        m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", self.model_id)
        return float(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Metadata de models.dev (la cache que mantiene OpenCode)
# --------------------------------------------------------------------------- #

MODELS_CACHE_ENV = "ORQ_MODELS_CACHE"


def models_cache_path() -> Path:
    raw = os.environ.get(MODELS_CACHE_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "opencode" / "models.json"


@dataclass(frozen=True)
class ModelMeta:
    """Lo que OpenCode ya sabe de un modelo. None = no hay dato, no es False."""

    tool_call: bool | None = None
    context: int | None = None
    variants: tuple[str, ...] = ()
    variants_known: bool = False  # False: el modelo declara variantes que no se pueden enumerar
    input_price: float | None = None   # USD por 1M tokens
    output_price: float | None = None


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


_META_CACHE: dict[tuple[str, int], dict[tuple[str, str], ModelMeta]] = {}


def _parse_models_meta(data: dict) -> dict[tuple[str, str], ModelMeta]:
    meta: dict[tuple[str, str], ModelMeta] = {}
    for provider, spec in data.items():
        if not isinstance(spec, dict):
            continue
        models = spec.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                continue
            limit = entry.get("limit")
            context = limit.get("context") if isinstance(limit, dict) else None
            options = entry.get("reasoning_options")
            variants: list[str] = []
            variants_known = True
            if isinstance(options, list) and options:
                effort = [o for o in options if isinstance(o, dict) and o.get("type") == "effort"]
                if effort:
                    for option in effort:
                        variants.extend(str(v) for v in (option.get("values") or []))
                else:
                    # toggle / budget_tokens: existen variantes pero no sabemos sus ids.
                    variants_known = False
            tool_call = entry.get("tool_call")
            cost = entry.get("cost")
            input_price = output_price = None
            if isinstance(cost, dict):
                input_price = _number(cost.get("input"))
                output_price = _number(cost.get("output"))
            meta[(str(provider), str(model_id))] = ModelMeta(
                tool_call=tool_call if isinstance(tool_call, bool) else None,
                context=context if isinstance(context, int) else None,
                variants=tuple(dict.fromkeys(variants)),
                variants_known=variants_known,
                input_price=input_price,
                output_price=output_price,
            )
    return meta


def load_models_meta(path: Path | None = None) -> dict[tuple[str, str], ModelMeta]:
    """Lee la cache de models.dev. Devuelve {} si no existe o esta corrupta."""
    target = path or models_cache_path()
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        return {}
    key = (str(target), stamp)
    if key not in _META_CACHE:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        _META_CACHE.clear()  # solo interesa la cache vigente
        _META_CACHE[key] = _parse_models_meta(data) if isinstance(data, dict) else {}
    return _META_CACHE[key]


def meta_for(ref: str | None) -> ModelMeta | None:
    """Metadata de un `provider/model` (ignora el sufijo #variant).

    Si el proveedor no aparece en models.dev (proxies, gateways propios), se acepta
    el modelo cuando ese id existe en un unico proveedor del catalogo. Si hay varios
    con datos distintos no se adivina: mejor un precio supuesto que uno equivocado.
    """
    if not ref:
        return None
    base = ref.split("#", 1)[0].strip()
    provider, _, model_id = base.partition("/")
    if not model_id:
        return None
    table = load_models_meta()
    found = table.get((provider, model_id))
    if found is not None:
        return found
    matches = {meta for (_provider, mid), meta in table.items() if mid == model_id}
    return matches.pop() if len(matches) == 1 else None


def tool_support(model: ModelInfo) -> bool | None:
    """True/False si hay dato; None si no se puede verificar (modelo local, por ejemplo)."""
    if model.capabilities and "tools" in model.capabilities:
        return bool(model.capabilities["tools"])
    meta = meta_for(model.ref)
    return meta.tool_call if meta is not None else None


def variant_support(ref: str, variant: str) -> bool | None:
    """True/False si la variante existe; None si no hay datos para afirmarlo."""
    meta = meta_for(ref)
    if meta is None or not meta.variants_known:
        return None
    return variant in meta.variants


def extract_providers(config: dict, source_config: Path | None = None) -> dict[str, Provider]:
    """Soporta `providers` (plural, guia) y `provider` (singular, variantes)."""
    raw: dict = {}
    for key in ("providers", "provider"):
        value = config.get(key)
        if isinstance(value, dict):
            raw = _deep_merge(raw, value)
    providers: dict[str, Provider] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        options = spec.get("options") or spec.get("settings") or {}
        models = spec.get("models") or {}
        providers[name] = Provider(
            name=name,
            display_name=spec.get("name") or name,
            base_url=options.get("baseURL") or options.get("baseUrl") or spec.get("baseURL"),
            api_key_present=bool(options.get("apiKey") or options.get("api_key")),
            declared_models=models if isinstance(models, dict) else {},
            source_config=source_config,
        )
    return providers


# --------------------------------------------------------------------------- #
# Sondeo de endpoints OpenAI-compatible
# --------------------------------------------------------------------------- #


def probe_models(base_url: str, timeout: float = 2.0) -> list[str]:
    """GET {base_url}/models -> lista de ids de modelo. Vacio si no responde."""
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    try:
        req = urllib.request.Request(f"{url}/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("id"):
            ids.append(str(entry["id"]))
        elif isinstance(entry, str):
            ids.append(entry)
    return sorted(set(ids))


# --------------------------------------------------------------------------- #
# Catalogo de precios (USD por 1M tokens: entrada/salida)
# --------------------------------------------------------------------------- #

CATALOG_PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-astra": (10.0, 50.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-codex": (1.25, 10.0),
    "gpt-6-luna": (0.10, 0.50),
    "deepseek-v4-flash": (0.15, 0.60),
    "deepseek-v3": (0.27, 1.10),
    "qwen3-max": (1.20, 6.00),
    "qwen3-coder-plus": (1.00, 5.00),
    "qwen3-235b": (0.20, 0.60),
    "llama-3.3-70b": (0.59, 0.79),
    "ministral-8b": (0.10, 0.10),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
}

# Palabras clave -> tier sugerido cuando no hay precio conocido.
TIER_HINTS: list[tuple[str, str]] = [
    ("flash", "cheap"), ("mini", "cheap"), ("nano", "cheap"), ("lite", "cheap"),
    ("haiku", "cheap"), ("small", "cheap"), ("turbo", "cheap"), ("instant", "cheap"),
    ("max", "frontier"), ("ultra", "frontier"), ("pro", "frontier"),
    ("opus", "frontier"), ("sonnet", "frontier"), ("gpt-5", "frontier"),
    ("gpt-6", "frontier"), ("astra", "frontier"), ("glm-4.6", "frontier"),
]

CHEAP_MAX_PRICE = 1.0  # USD/1M entrada: a partir de aqui es "barato"

# Modelos que aparecen en /v1/models pero no sirven para un agente con tools:
# embeddings, TTS, ASR,rerank. No deben acabar en un campo "model:".
NON_CHAT_PATTERNS = (
    "embed", "embedding", "bge-", "gte-", "e5-", "nomic-embed", "whisper",
    "tts", "kokoro", "piper", "voice", "speech", "asr", "rerank", "diarize",
    "clip", "whisperx", "parler", "mms-", "wav2vec", "audio", "speecht5",
)


def is_chat_model(model_id: str) -> bool:
    low = model_id.lower()
    return not any(pattern in low for pattern in NON_CHAT_PATTERNS)


def known_price(model_id: str) -> tuple[float, float] | None:
    low = model_id.lower()
    for key, price in CATALOG_PRICES.items():
        if key in low:
            return price
    return None


# Coste por 1M tokens para un modelo del que no se conoce el precio. No es real:
# sirve para ordenar y para que la UI no muestre huecos, y se marca como estimacion.
ASSUMED_PRICE: tuple[float, float] = (1.0, 3.0)


@dataclass
class Pricing:
    input: float
    output: float
    exact: bool  # False = precio supuesto o solo del catalogo embebido
    source: str = ""  # models.dev | catalogo embebido | supuesto | local


def price_of(model: ModelInfo, provider: Provider | None = None) -> Pricing | None:
    """Precio por 1M tokens, o None si el modelo es local (coste cero).

    Prioridad: local > models.dev > catalogo embebido > precio supuesto. Solo los
    dos primeros se consideran exactos; el resto sirve para comparar escenarios.
    """
    if provider is not None and provider.is_local:
        return Pricing(0.0, 0.0, exact=True, source="local")
    meta = meta_for(model.ref)
    if meta is not None and meta.input_price is not None and meta.output_price is not None:
        return Pricing(meta.input_price, meta.output_price, exact=True, source="models.dev")
    price = known_price(model.model_id)
    if price:
        return Pricing(price[0], price[1], exact=False, source="catalogo embebido")
    return Pricing(ASSUMED_PRICE[0], ASSUMED_PRICE[1], exact=False, source="supuesto")


def blended_price(model: ModelInfo, provider: Provider | None = None) -> float:
    """Coste medio por 1M tokens contando ~80% entrada / 20% salida (razonamiento)."""
    price = price_of(model, provider)
    if price is None:
        return 0.0
    return price.input * 0.8 + price.output * 0.2


def guess_tier(model: ModelInfo, provider: Provider | None = None) -> str:
    """frontier | cheap | local"""
    if provider is not None and provider.is_local:
        return "local"
    meta = meta_for(model.ref)
    if meta is not None and meta.input_price is not None:
        # Precio real publicado: es el mejor indicador de tier que tenemos.
        return "cheap" if meta.input_price <= CHEAP_MAX_PRICE else "frontier"
    price = known_price(model.model_id)
    if price:
        return "cheap" if price[0] <= CHEAP_MAX_PRICE else "frontier"
    low = model.model_id.lower()
    for token, tier in TIER_HINTS:
        if token in low:
            return tier
    # Heuristica por tamano: >= 30B se considera frontera, < 7B barato.
    params = model.params_b
    if params:
        if params >= 30:
            return "frontier"
        if params <= 7:
            return "cheap"
    if provider is not None and provider.is_local:
        return "local"
    return "cheap"


# --------------------------------------------------------------------------- #
# Catalogo final
# --------------------------------------------------------------------------- #


@dataclass
class Catalog:
    providers: dict[str, Provider]
    models: list[ModelInfo]
    probed: dict[str, list[str]]
    excluded: list[ModelInfo] = field(default_factory=list)

    def by_tier(self, tier: str) -> list[ModelInfo]:
        out = []
        for model in self.models:
            provider = self.providers.get(model.provider)
            if guess_tier(model, provider) == tier:
                out.append(model)
        return out

    def find(self, ref: str) -> ModelInfo | None:
        for model in self.models:
            if model.ref == ref or model.model_id == ref:
                return model
        return None

    def probes_available(self) -> list[Provider]:
        return [p for p in self.providers.values() if p.base_url]


def build_catalog(
    config: dict,
    sources: list[Path],
    project_dir: Path | None = None,
    probe: bool = True,
    timeout: float = 2.0,
    extra_endpoints: list[str] | None = None,
) -> Catalog:
    providers: dict[str, Provider] = {}
    if config:
        providers.update(extract_providers(config))
    for path in sources:
        for name, provider in extract_providers(load_config_file(path), path).items():
            existing = providers.get(name)
            if existing is None or (existing.base_url is None and provider.base_url):
                providers[name] = provider

    models: list[ModelInfo] = []
    seen: set[str] = set()
    for provider in providers.values():
        for model_id, meta in provider.declared_models.items():
            ref = f"{provider.name}/{model_id}"
            if ref in seen:
                continue
            seen.add(ref)
            models.append(
                ModelInfo(
                    provider=provider.name,
                    model_id=model_id,
                    display_name=(meta or {}).get("name") or model_id,
                    source="config",
                    context=((meta or {}).get("limit") or {}).get("context"),
                    capabilities=(meta or {}).get("capabilities") or {},
                )
            )

    probed: dict[str, list[str]] = {}
    excluded: list[ModelInfo] = []
    targets = list(extra_endpoints or [])
    for provider in providers.values():
        if provider.base_url:
            targets.append(provider.base_url)
    for base in targets:
        found = probe_models(base, timeout=timeout) if probe else []
        probed[base] = found
        if not found:
            continue
        owner = next(
            (p for p in providers.values() if p.base_url and p.base_url.rstrip("/").startswith(base.rstrip("/"))),
            None,
        )
        provider_name = owner.name if owner else _provider_name_for(base)
        providers.setdefault(
            provider_name,
            Provider(name=provider_name, display_name=provider_name, base_url=base),
        )
        for model_id in found:
            ref = f"{provider_name}/{model_id}"
            if not is_chat_model(model_id):
                excluded.append(
                    ModelInfo(provider=provider_name, model_id=model_id, source="probe")
                )
                continue
            if ref in seen:
                continue
            seen.add(ref)
            models.append(ModelInfo(provider=provider_name, model_id=model_id, source="probe"))

    # Filtra tambien los declarados en config: un embedding no es un agente.
    kept: list[ModelInfo] = []
    for model in models:
        if not is_chat_model(model.model_id):
            excluded.append(model)
            continue
        if model.context is None:
            meta = meta_for(model.ref)
            if meta is not None and meta.context:
                model.context = meta.context
        kept.append(model)

    return Catalog(providers=providers, models=kept, probed=probed, excluded=excluded)


def _provider_name_for(base_url: str) -> str:
    host = re.sub(r"^https?://", "", base_url).split("/")[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")
    return slug or "local"