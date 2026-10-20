# orquestador (`orq`)

Genera un **orquestador de agentes para OpenCode V2** específico para un proyecto concreto,
eligiendo los modelos que realmente tienes disponibles en tu máquina y en tu red.

La idea economicamente: **no pagues un modelo frontera por leer un fichero**. El
orquestador (modelo caro) solo decide y delega; el trabajo mecanico lo hacen un modelo
local (gratis y privado) o uno barato en la nube (~100x mas barato). Ademas, cada subagente
trabaja en su propia sesion, asi que los ficheros que lee **no vuelven al contexto caro**.

Seguiendo la guia [Orquestador de agentes (ai.manz.dev)](https://ai.manz.dev/learn/setup/opencode-orquestador/).

## Que hace

1. **Escanea el proyecto**: lenguaje, stack (Laravel, Python, Node, Rust...), comandos de
   test y lint, carpetas clave y ficheros sensibles (`.env`, claves).
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

Los agentes generados son:

| Agente | Modelo | Que hace |
| --- | --- | --- |
| `orquestador` | frontera | Decide, descompone, delega. `edit`/`write`/`shell` denegados: no puede tocar nada. |
| `local` | local | Leer, buscar, editar en masa. `webfetch`/`websearch` y ficheros `.env` denegados: nada sale de la maquina. |
| `barato` | barato | Renombrados, formateo, redaccion simple. `edit`/`shell` en `ask`. |
| `especialista` | frontera | Trabajo profundo de dominio (Laravel, Python...). Puede editar, pero delega lo mecanico en `local`/`barato` para no gastar su contexto caro. |

El reparto por defecto del trabajo es **50% local / 30% barato / 15% especialista /
5% orquestador**, ajustable con `--local-share` / `--cheap-share` / `--specialist-share`.
Con el reparto por defecto, un plan mixto (frontera + barato + local) sale en torno a
un 90% mas barato que hacer todo con el modelo caro: ese es el argumento de la guia,
en numero.

> Si no hay ningun modelo de frontera ni barato disponibles, el orquestador cae al
> modelo local que haya y lo avisa: un orquestador sin `model` heredaria el de la sesion
> padre, que es justo el gasto que esto evita.

## Instalacion

Global, para poder ejecutarlo desde cualquier directorio:

```bash
uv tool install -e .
orq --version
```

Queda disponible como `orq` en `~/.local/bin`. Al ser una instalacion **editable**, el
comando global apunta al codigo del repo: si editas `src/orquestador/` y vuelves a
ejecutar `orq`, ya tienes el cambio. No hay que reinstalar nada.

Para desarrollo (tests, sincronizar deps):

```bash
cd /ruta/al/repo
uv sync
uv run pytest
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

Te muestra el stack, los modelos detectados y te deja elegir cual va a cada rol.

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

### Informacion

```bash
orq detect             # stack del proyecto + catalogo de modelos
orq models             # solo modelos, tiers y seleccion automatica
orq agents             # que agentes hay y a quien puede llamar cada uno
orq agents --show local   # imprime el prompt completo de un agente
orq check              # valida lo generado (falla si un modelo no existe o no soporta tools)
orq compare            # estimacion de coste frente a hacerlo todo con el modelo caro
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
```

Compara el coste del plan con el de "que lo haga todo el padre" y muestra el ahorro.
Los precios vienen de un catalogo embebido (marcados con `*` cuando son estimados),
asi que sirve para **comparar escenarios entre si**, no para predecir una factura.

### Global (todos tus proyectos)

```bash
orq init --scope global     # escribe en ~/.config/opencode/agents/
```

## Seguridad de la configuración

`--force` reescribe **solo los ficheros de agente**. Tu `opencode.jsonc` nunca se
sobrescribe a ciegas, porque ahí viven tus providers, MCP y credenciales:

- Si no existe, se crea con `default_agent`.
- Si existe y es JSON válido, se le añade `default_agent` conservando el resto.
- Si tiene comentarios JSONC, **no se toca** y te dice qué línea añadir a mano,
  porque reserializarlo los destruiría.

### Permisos de los agentes

Los cuatro agentes generados comparten una politica minima, ademas de la que ya
describe la tabla de arriba:

- `external_directory` denegado: no leen ni escriben fuera del proyecto.
- Los ficheros sensibles detectados (`.env`, claves, certificados) se deniegan para
  `read` y `edit` en todos, incluido el agente `barato` (que va a la nube).
- El agente `local` deniega los comandos de red (`curl`, `wget`, `nc`, `ssh`, `scp`,
  `git push`). Es un guardarrail contra fugas accidentales, no un sandbox.
- El orquestador no lee secretos: su contexto se resume al plan.

## Estrategia: `.orquestador.yaml`

Se genera automaticamente y es la fuente de verdad para re-generar. Editalo a tu gusto:

```yaml
orchestrator_model: openai/gpt-6-astra
orchestrator_variant: auto
default_agent: true
scope: project
subagents:
  local:
    model: lemonade/Qwen3.8-27B-GGUF-UD-Q4_K_XL
    steps: 40
  barato:
    model: openai/gpt-6-luna
    steps: 12
  especialista:
    model: openai/gpt-6-astra
    steps: 30
rules:
  - "Para migraciones de base de datos, delega siempre en especialista"
```

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
uv run pytest
```

## Por que Python y no Rust

El trabajo de I/O (leer JSON/JSONC,sondear HTTP,escribir markdown) y plantillas. Python
da tipado estatico con `dataclasses`, el ecosistema (`typer`, `rich`, `pyyaml`) y una velocidad de
iteracion que en Rust costaria el doble sin ganar nada aqui. Si en el futuro hiciera falta
distribuir un binario unico, `uv build --script` o PyInstaller resuelven sin reescribir.