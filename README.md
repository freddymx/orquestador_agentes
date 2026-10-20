# orquestador (`orq`)

Genera un **orquestador de agentes para OpenCode V2** específico para un proyecto concreto,
eligiendo los modelos que realmente tienes disponibles en tu máquina y en tu red.

La idea economicamente: **no pagues un modelo frontera por leer un fichero**. El
orquestador (modelo caro) solo decide y delega; el trabajo mecanico lo hacen un modelo
local (gratis y privado) o uno barato en la nube (~100x mas barato). Ademas, cada subagente
trabaja en su propia sesion, asi que los ficheros que lee **no vuelven al contexto caro**.

Seguiendo la guia [Orquestador de agentes (ai.manz.dev)](https://ai.manz.dev/learn/setup/opencode-orquestador/).

## Que hace

1. **Escanea el proyecto**: lenguaje, **runtimes y frameworks reales** (Laravel, Next.js,
   FastAPI, Django, Gin...), comandos de test/lint/build leídos de los scripts del proyecto,
   carpetas clave y ficheros sensibles (`.env`, claves).
2. **Descubre los modelos**: lee `opencode.json(c)` de proyecto y global (soporta `providers`
   y `provider`), y **sondea** los endpoints OpenAI-compatible (`llama.cpp`, Lemonade,
   Ollama...) para listar los modelos que realmente sirven.
3. **Clasifica cada modelo en un tier** (`frontera` / `barato` / `local`) usando precios
   conocidos, nombre del modelo y si el endpoint es local.
4. **Planea los agentes**: decide que subagentes hacen falta segun el proyecto y los tiers
   disponibles, con los permisos correctos para cada uno.
5. **Escribe** `.opencode/agents/*.md` + `opencode.jsonc` y guarda la estrategia en
   `.orquestador.yaml` para poder re-generarla tras cambiarla a mano.
6. **Valida** el resultado: modelos inexistentes, orquestador que puede editar, permisos que
   apuntan a agentes inexistentes, etc.

Los agentes fijos generados son:

| Agente | Modelo | Que hace |
| --- | --- | --- |
| `orquestador` | frontera | Decide, descompone, delega. `edit`/`shell` denegados: no puede tocar nada. |
| `local` | local | Leer, buscar, editar en masa. `webfetch`/`websearch` y ficheros `.env` denegados: nada sale de la maquina. |
| `barato` | barato | Renombrados, formateo, redaccion simple. `edit`/`shell` en `ask`. |
| `especialista` | frontera | Trabajo profundo de dominio (Laravel, Python...). Puede editar, pero delega lo mecanico en `local`/`barato` para no gastar su contexto caro. |

Ademas puedes declarar **subagentes personalizados** (por ejemplo `frontend`, `seguridad`,
`migraciones`) con su propio modelo, prompt y permisos; el orquestador los enruta igual.
Mira [presets y subagentes](#presets-de-jerarquia).

El reparto por defecto del trabajo es **50% local / 30% barato / 15% especialista /
5% orquestador**, ajustable con `--local-share` / `--cheap-share` / `--specialist-share`.
Los subagentes personalizados comparten la parte de su `tier` (por ejemplo, dos agentes
`cheap` se reparten el 30% de `barato`).
El calculo usa **un unico presupuesto de tokens por sesion** (100k por defecto) y
reparte la parte de los roles que no existan entre los presentes, para que el ahorro
compare el mismo trabajo. Con precios reales de models.dev, un plan mixto suele salir
entre un **60% y un 80%** mas barato que hacerlo todo con el modelo caro; la cifra
exacta depende de las tarifas de tus modelos (`orq compare`).

> Si no hay ningun modelo de frontera ni barato disponibles, el orquestador cae al
> modelo local que haya y lo avisa: un orquestador sin `model` heredaria el de la sesion
> padre, que es justo el gasto que esto evita.

## Deteccion de stack

No adivina por el nombre del directorio: lee los manifiestos y separa **runtime** de
**framework**. Un `composer.json` cualquiera ya no se etiqueta como Laravel; hace falta
`laravel/framework` o un `artisan` real.

- **PHP**: `composer.json` (`require`/`require-dev` y `scripts`), `pest.php`, `phpunit.xml`,
  `pint.json`, `.php-cs-fixer.php`, `phpcs.xml`.
- **Node/TS**: `package.json` (dependencias y scripts), `pnpm-lock.yaml`/`yarn.lock`/`bun.lock`,
  configs de vitest/jest/playwright, ESLint/Biome.
- **Python**: `pyproject.toml` (`project`, `dependency-groups`, Poetry, `[tool.pytest]`,
  `[tool.ruff]`), `requirements*.txt`, `Pipfile`; usa `uv run` solo si hay `uv.lock`.
- **Rust/Go/Ruby/JVM/.NET/Swift/Elixir**: `Cargo.toml`, `go.mod`, `Gemfile`, Gradle/Maven,
  `.csproj`/`.sln`, `Package.swift`, `mix.exs`.
- Si no hay comando de ecosistema, prueba objetivos `test`/`lint`/`build` en el `Makefile`.

Los comandos solo se proponen si el script existe: no veras `npm run lint` en un proyecto
sin script `lint`. `orq detect --json` expone `runtimes`, `frameworks` y los tres comandos
por separado.

## Instalacion

Global, para poder ejecutarlo desde cualquier directorio:

```bash
uv tool install -e .
orq --version
```

Queda disponible como `orq` en `~/.local/bin`. Al ser una instalacion **editable**, el
comando global apunta al codigo del repo: si editas `src/orquestador/` y vuelves a
ejecutar `orq`, ya tienes el cambio. No hay que reinstalar nada.

Para desarrollo (tests, lint, tipos, sincronizar deps):

```bash
cd /ruta/al/repo
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

Si mas adelante quieres una copia congelada en vez de editar el repo, quita el `-e`:
`uv tool install .`

## Uso

Siempre puedes omitir `-d`: por defecto opera sobre el directorio actual.

### Interactivo (recomendado la primera vez)

```bash
cd /ruta/de/tu/proyecto
orq init
```

Te muestra el stack, los modelos detectados, te deja elegir el **preset de jerarquia**
(`balanced`, `lean` o `strict`) y cual va a cada rol. Tambien puedes pasarlo directo:
`orq init --preset lean`.

Si algún tier está vacío —típico si solo tienes modelos locales—, **no se salta la
pregunta**: te ofrece todos los modelos disponibles marcados con su tier real y tú
decides. Puedes dejar un rol en `0` si prefieres que ese trabajo caiga en otro agente,
menos el orquestador, que siempre necesita un modelo fijo.

### No interactivo

```bash
orq generate --orchestrator-model openai/gpt-6-astra#high \
             --cheap-model openai/gpt-6-luna#low \
             --local-model lemonade/Qwen3.8-27B-GGUF-UD-Q4_K_XL
```

`--preset balanced|lean|strict` cambia la jerarquia sin tocar los modelos. Se guarda en
`.orquestador.yaml`, asi que las siguientes ejecuciones lo respetan.

### Informacion

```bash
orq detect             # stack del proyecto + catalogo de modelos
orq models             # solo modelos, tiers y seleccion automatica
orq agents             # que agentes hay y a quien puede llamar cada uno
orq agents --show local   # imprime el prompt completo de un agente
orq check              # valida lo generado (falla si un modelo no existe o no soporta tools)
orq compare            # estimacion de coste frente a hacerlo todo con el modelo caro
orq diff               # que cambiaria `orq generate`, sin escribir nada (exit 1 si hay cambios)
orq generate --dry-run # imprime el contenido de los agentes sin escribir
```

Todos los comandos de informacion aceptan `--json` para scripting, y `--no-probe`
para no sondear la red (util en CI o sin LAN):

```bash
orq detect --json | jq '.models[] | select(.tier=="local")'
orq check --json | jq -r '.ok'
```

### Coste estimado

`orq compare` responde a la pregunta de "esto sirve o no", con el reparto de trabajo
que has configurado (por defecto 50% local / 30% barato / 15% especialista / 5%
orquestador):

```bash
orq compare --sessions 20
orq compare --sessions 20 --local-share 0.9 --cheap-share 0.05
orq compare --tokens 250000 --json
```

Compara el coste del plan con el de "que lo haga todo el padre" y muestra el ahorro.
Los precios salen de **models.dev** (la cache que mantiene OpenCode) cuando el modelo
aparece alli; si no, del catalogo embebido o de un valor supuesto marcado con `*`.
Sirve para **comparar escenarios con el mismo trabajo**, no para predecir una factura:
los precios por tramos de contexto o cache no se modelan.

### Global (todos tus proyectos)

```bash
orq init --scope global     # escribe en ~/.config/opencode/agents/
```

## Seguridad de la configuración

`--force` reescribe **solo los ficheros de agente**. Tu `opencode.jsonc` nunca se
sobrescribe a ciegas, porque ahí viven tus providers, MCP y credenciales:

- Si no existe, se crea con `default_agent`.
- Si existe, se le añade o actualiza `default_agent` **editando el texto**: los
  comentarios `//` y `/* */`, el orden de claves, tus providers y MCP se conservan
  exactamente como estaban. No se reserializa el fichero.
- Si el fichero no es JSONC válido, no se toca y te dice qué línea añadir a mano.

Todas las escrituras son atómicas (fichero temporal + rename), así que una interrupción
no deja agentes a medias. Para conservar la versión anterior antes de sobrescribir:

```bash
orq generate --force --backup   # deja <agente>.md.bak y opencode.jsonc.bak
```

`orq diff` usa exactamente el mismo cálculo que `orq generate` pero no escribe nada:
imprime un diff unificado por fichero y sale con código 1 si hay diferencias (0 si está
al día). Es la forma barata de revisar un cambio de estrategia en CI o antes de aplicar:

```bash
orq diff
orq diff --json | jq '.files[] | select(.status != "unchanged")'
```

### Permisos de los agentes

Los agentes generados (incluidos los personalizados) comparten una politica minima,
ademas de la que ya describe la tabla de arriba:

- `external_directory` denegado: no leen ni escriben fuera del proyecto.
- Los ficheros sensibles detectados (`.env`, claves, certificados) se deniegan para
  `read` y `edit` en todos, incluido el agente `barato` (que va a la nube).
- El agente `local` deniega los comandos de red (`curl`, `wget`, `nc`, `ssh`, `scp`,
  `git push`). Es un guardarrail contra fugas accidentales, no un sandbox.
- El orquestador no lee secretos: su contexto se resume al plan.
- `preset: strict` ademas quita el shell a todos los subagentes, hace que las ediciones
  de los agentes cloud pidan confirmacion y evita que el orquestador navegue por la web.
- En un subagente personalizado, `permissions: read` deniega `edit` y `shell`;
  `edit` (por defecto) permite editar y pide confirmacion para shell; `full` permite
  ambos. Los secretos se deniegan siempre, incluso con `full`.

## Estrategia: `.orquestador.yaml`

Se genera automaticamente y es la fuente de verdad para re-generar. Editalo a tu gusto:

```yaml
preset: balanced
orchestrator_model: openai/gpt-6-astra
orchestrator_variant: auto
default_agent: true
scope: project
subagents:
  local:
    model: lemonade/Qwen3.8-27B-GGUF-UD-Q4_K_XL
    steps: 40
    enabled: true
  barato:
    model: openai/gpt-6-luna
    steps: 12
  especialista:
    model: openai/gpt-6-astra
    steps: 30
  frontend:
    model: auto          # elige por tier; tambien acepta provider/model#variant
    tier: cheap          # local | cheap | frontier
    steps: 18
    description: "Implementa y revisa UI"
    prompt: "Trabaja solo en src/components. No toques el backend."
    permissions: edit    # read | edit (por defecto) | full
rules:
  - "Para migraciones de base de datos, delega siempre en especialista"
```

### Presets de jerarquia

`preset` decide cuantos agentes se generan y como se endurecen:

- **`balanced`** (por defecto): orquestador + `local`, `barato`, `especialista` y todos
  los subagentes personalizados que añadas.
- **`lean`**: orquestador + **un solo** trabajador, por orden `local` > `barato` >
  `especialista`. Los demas roles fijos y los personalizados se omiten. Ideal para
  proyectos pequeños o para no llenar el selector de agentes.
- **`strict`**: misma jerarquia que `balanced`, pero los subagentes no tienen shell, las
  ediciones de los agentes cloud piden confirmacion y el orquestador no puede navegar.

`auto`, `default` o dejar la clave vacia equivalen a `balanced`. Tambien puedes cambiar el
preset sin editar el YAML:

```bash
orq generate --preset lean
orq compare --preset strict --sessions 10
```

### Subagentes personalizados

Cualquier clave bajo `subagents` que no sea `local`, `barato`, `especialista` (o su alias
`specialist`) se trata como un agente propio. El nombre debe empezar por letra, usar solo
`[a-z0-9_-]` (max. 40) y no puede coincidir con `orquestador`, `local`, `barato` ni
`especialista`.

Campos:

- `model`: `provider/model` (con `#variant` opcional) o `auto`. Si falta, se elige por
  `tier`.
- `tier`: `local`, `cheap` o `frontier`. Sirve para elegir modelo automatico y para
  repartir el presupuesto de tokens en `orq compare`.
- `steps`: entero positivo (20 por defecto).
- `enabled: false`: no se genera ni se ofrece al orquestador; su parte del coste se
  reparte entre el resto.
- `description`: una linea; es lo que ve el orquestador para decidir a quien delegar.
- `prompt`: instrucciones propias del agente. Si no lo pones, se usa un prompt generico.
- `permissions`: `read` (solo lectura, sin shell), `edit` (editar y pedir confirmacion
  para shell) o `full` (editar y shell).
- `color`: hex `#rrggbb` opcional.

El orquestador recibe automaticamente permiso para delegar en cada subagente
personalizado y una linea de enrutado con su descripcion. Todos heredan la politica de
seguridad comun: sin `external_directory`, sin secretos y sin delegar en otros agentes.

Cada subagente acepta `enabled: false`: no se genera su `.md`, el orquestador no lo
ofrece como delegado y su parte del trabajo se reparte entre los que quedan (también en
`orq compare`). Sirve, por ejemplo, para un portátil sin modelo local o para no enviar
nada a la nube con el agente `barato`. Si el `.md` ya existía de una generación anterior,
`orq` no lo borra por su cuenta: te avisa para que lo elimines tú. Lo mismo aplica si
cambias a `preset: lean` o quitas un subagente personalizado de la estrategia: borra los
`.md` que ya no uses.

`--force` vuelve a escribir los agentes del plan actual, pero nunca borra los que quedan
fuera. `orq check` tambien los marca como "no esta en el plan actual".

Despues: `orq generate` (sin `--force` no pisa ficheros existentes; con `--force` los rehace).

`scope` se guarda en la estrategia, asi que `orq generate` reutiliza donde se escribio
la ultima vez; `--scope` solo hace falta para cambiarlo.

`orchestrator_variant: auto` usa `high` **solo si el modelo lo declara** en el catalogo
de OpenCode (`~/.cache/opencode/models.json`). Una variante que el modelo no soporta es
un error de resolucion en OpenCode, asi que nunca se inventa: si no hay dato, no se pone.
Puedes forzar una concreta (`high`, `low`...) o desactivarla con `none`.

## Anadir un modelo local

Si tu config ya declara el provider pero sin modelos, `orq` te imprime el bloque exacto
para pegar en `opencode.jsonc`:

```jsonc
"provider": {
  "llamacpp": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "llama.cpp",
    "options": { "baseURL": "http://localhost:8080/v1", "apiKey": "llamacpp" },
    "models": { "qwen3.6-35b": { "name": "Qwen 3.6 35B (local)" } }
  }
}
```

Y para descubrir modelos sin tocar la config:

```bash
orq models --endpoint http://localhost:8080/v1
```

## Tests

```bash
uv run pytest        # suite completa
uv run ruff check .  # errores reales (imports sin usar, nombres indefinidos...)
uv run mypy          # tipado de src/orquestador
```

La CI (`.github/workflows/ci.yml`) ejecuta las tres cosas en Python 3.11 y 3.12. Los tests
no leen la cache real de models.dev: `tests/conftest.py` la sustituye por un fichero vacio
y el fixture `models_cache` inyecta metadatos falsos.

## Por que Python y no Rust

El trabajo de I/O (leer JSON/JSONC,sondear HTTP,escribir markdown) y plantillas. Python
da tipado estatico con `dataclasses`, el ecosistema (`typer`, `rich`, `pyyaml`) y una velocidad de
iteracion que en Rust costaria el doble sin ganar nada aqui. Si en el futuro hiciera falta
distribuir un binario unico, `uv build --script` o PyInstaller resuelven sin reescribir.