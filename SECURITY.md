# Seguridad

`orq` es una herramienta local que genera configuracion para OpenCode V2. No es un
sandbox ni un servicio remoto.

## Reportar una vulnerabilidad

No abras un issue publico con detalles explotables. Usa
[GitHub Security Advisories](https://github.com/freddymx/orquestador_agentes/security/advisories/new)
o contacta con el maintainer por el canal privado que aparezca en el perfil de GitHub.

Incluye version, sistema operativo, pasos de reproduccion y el impacto esperado.

## Modelo de confianza

- `orq` lee `.orquestador.yaml`, `opencode.jsonc`, manifiestos y scripts del proyecto.
  Trata un repositorio no confiable como codigo: revisa esos ficheros antes de ejecutar
  `orq generate` o de usar los agentes generados.
- Los agentes personalizados con `permissions: full` pueden usar shell. Los denies de
  secretos y `external_directory` son guardarrailes, no aislamiento.
- Una estrategia de repo puede pedir `scope: global`; `generate` exige `--scope global`
  explicito para escribir en `~/.config/opencode`.
- El sondeo HTTP solo usa `http`/`https`, pero puede alcanzar endpoints internos declarados
  en la config. Usa `--no-probe` con repos no confiables.

## Credenciales

- `opencode.jsonc` puede contener `apiKey`, tokens y credenciales de MCP. `orq` no las
  imprime, no las reserializa y no hace `.bak` de ese fichero.
- `orq diff` omite el diff humano de `opencode.jsonc` para no volcar credenciales.
- El escaneo de secretos es heuristico y esta limitado (4000 ficheros, 50 rutas). No
  sustituye a una revision de seguridad ni a un gestor de secretos.

## Versiones

La rama `main` recibe las correcciones. Para uso en produccion, fija una release o un
commit concreto.
