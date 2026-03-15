# Guía: Iniciar en OpenClaw y correr agentes autónomos

> Pasos para instalar OpenClaw, configurar un agente y dejarlo corriendo de forma autónoma (por ejemplo en un servidor o VPS).

---

## 1. Qué es OpenClaw

OpenClaw es una plataforma para **agentes de IA** que pueden:
- Chatear por **Telegram**, WhatsApp, Discord, Slack, etc.
- Usar **herramientas** (bash, skills, APIs).
- Tener **identidad**, **memoria** y **documentación** (archivos .md en el workspace).

Un **Gateway** es el proceso que recibe mensajes de los canales y los pasa al agente. Para que el agente funcione “solo”, el Gateway debe estar corriendo de forma persistente (por ejemplo como servicio systemd).

---

## 2. Requisitos

- **Node.js 22+** (el instalador puede instalarlo si falta).
- **Sistema**: Linux, macOS o Windows (en Windows se recomienda WSL2).
- **Servidor** (para agente autónomo): un VPS (AWS EC2, Hetzner, Fly.io, etc.) con acceso SSH.

---

## 3. Instalación

### Opción recomendada: script oficial

En el servidor (o en tu máquina):

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
```

Eso instala el CLI y puede lanzar el asistente de configuración. Para **solo instalar** sin asistente:

```bash
curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard
```

### Opción: npm (si ya tienes Node 22+)

```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon
```

### Verificar

```bash
openclaw doctor
openclaw status
```

### Borrar el agente actual y ejecutar otro desde cero

Si tienes un agente **inactivo**, el gateway **no arranca** o quieres empezar limpio:

1. **Parar el servicio** (en el servidor):
   ```bash
   systemctl --user stop openclaw-gateway
   ```

2. **Borrar la instalación/config** del agente (todo lo que OpenClaw usa):
   ```bash
   rm -rf ~/.openclaw
   ```
   Los archivos .md importantes siguen en tu repo (`docs/agent/`); no se pierden.

3. **Volver a instalar y configurar** en la misma máquina:
   ```bash
   curl -fsSL https://openclaw.ai/install.sh | bash
   ```
   o, si ya tienes el CLI:
   ```bash
   openclaw setup
   openclaw onboard --install-daemon
   ```
   El onboarding te pide de nuevo: modelo (API key), gateway (puerto, contraseña), canales (Telegram, etc.). Es como tener un **agente nuevo**.

4. **Subir de nuevo la documentación** del agente (desde tu Mac, en el repo):
   ```bash
   ./deploy-agent-docs.sh
   ```

5. **Arrancar el gateway**:
   ```bash
   systemctl --user start openclaw-gateway
   ```

Con eso tienes un solo agente “nuevo” corriendo; no hace falta “borrar un agente” y “crear otro” por separado: borrar `~/.openclaw` y volver a hacer setup + onboard **es** empezar con otro.

---

## 4. Configuración inicial

### 4.1 Crear workspace y config base

```bash
openclaw setup
```

Crea `~/.openclaw/openclaw.json` y el workspace por defecto (`~/.openclaw/workspace`).

O con wizard interactivo:

```bash
openclaw setup --wizard
```

### 4.2 Asistente de onboarding (auth, gateway, canales)

```bash
openclaw onboard --install-daemon
```

El wizard te pide:
- **Autenticación del modelo** (API key de OpenAI, Anthropic, etc.). Las claves se guardan en `~/.openclaw/agents/<agentId>/agent/auth-profiles.json`.
- **Gateway**: puerto (por defecto 18789), contraseña o token si quieres proteger el acceso.
- **Canales** (opcional): Telegram, WhatsApp, etc. Para Telegram necesitas un bot creado con @BotFather y el token.

### 4.3 Config mínima (openclaw.json)

Si prefieres editar a mano, un mínimo razonable:

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.openclaw/workspace"
    }
  },
  "gateway": {
    "port": 18789,
    "auth": {
      "mode": "password",
      "password": "TU_CONTRASEÑA_SEGURA"
    }
  },
  "channels": {
    "telegram": {
      "accounts": {
        "default": {
          "botToken": "TU_TOKEN_DE_BOTFATHER"
        }
      }
    }
  }
}
```

Sustituye `TU_CONTRASEÑA_SEGURA` y `TU_TOKEN_DE_BOTFATHER`. Documentación completa: [Configuration](https://docs.clawd.bot/gateway/configuration).

---

## 5. Documentación del agente (archivos .md)

OpenClaw usa archivos en el **workspace** y en la carpeta del **agente** para dar contexto al modelo:

| Archivo      | Ubicación típica | Uso |
|-------------|-------------------|-----|
| **TOOLS.md** | `~/.openclaw/workspace/TOOLS.md` | Herramientas disponibles (Git, Twitter, etc.). |
| **IDENTITY.md** | `~/.openclaw/agents/main/agent/IDENTITY.md` | Nombre, rol y presentación del agente. |
| MEMORY.md, SKILLS.md, SOUL.md, USER.md, HEARTBEAT.md, PROJECTS.md, GUIDELINES.md, CONTACTS.md, GOALS.md | `~/.openclaw/workspace/` | Contexto, preferencias, objetivos, procedimientos. |

En este repo tienes todo eso en **docs/agent/**. Para subirlo al servidor:

```bash
./deploy-agent-docs.sh
```

(Requiere SSH configurado al host; ver README en docs/agent.)

---

## 6. Ejecutar el Gateway

### 6.0 Pasar de Trading a Community Manager (instancia IRLANDA-PUMA)

Si la instancia tenía servicios de trading (pumaclaw-hedger, listener, etc.) y quieres usar **solo** el agente como Community Manager:

**En la instancia (SSH):**

```bash
# 1. Parar el servicio del gateway (evita conflictos con procesos huérfanos)
systemctl --user stop openclaw-gateway.service

# 2. Matar cualquier proceso que siga usando el puerto 18789 (gateway viejo huérfano)
sudo lsof -ti:18789 | xargs -r sudo kill -9
# o si no tienes lsof:  kill -9 688987   # (usa el PID que salga en "gateway already running (pid ...)")

# 3. Desactivar y parar los servicios de trading
systemctl --user stop pumaclaw-blind.service pumaclaw-hedger.service pumaclaw-listener.service pumaclaw-report.service pumaclaw-trader.service
systemctl --user disable pumaclaw-blind.service pumaclaw-hedger.service pumaclaw-listener.service pumaclaw-report.service pumaclaw-trader.service

# 4. Arrancar solo el gateway (Community Manager)
systemctl --user start openclaw-gateway.service

# 5. Comprobar que está corriendo
openclaw gateway status
# o:  systemctl --user status openclaw-gateway.service
```

**Si el gateway sigue sin arrancar** y el log dice `set gateway.mode=local (current: unset)`:

En la instancia, edita `~/.openclaw/openclaw.json` y añade dentro de `"gateway"` la línea `"mode": "local",` (junto a `"port"`). Ejemplo:

```json
"gateway": {
  "mode": "local",
  "port": 18789,
  ...
}
```

Guarda, luego: `systemctl --user start openclaw-gateway.service` y `openclaw gateway status`.

Para que el gateway arranque al reiniciar la máquina (opcional):

```bash
systemctl --user enable openclaw-gateway.service
```

---

### En primer plano (pruebas)

```bash
openclaw gateway --port 18789
```

Se detiene al cerrar la terminal.

### Como daemon / servicio (agente autónomo)

Si usaste el onboarding con `--install-daemon`, ya se instaló un servicio (systemd/launchd). Comandos típicos:

```bash
# Ver estado
openclaw gateway status

# Iniciar
openclaw gateway start
# o, en Linux con systemd user:
systemctl --user start openclaw-gateway

# Parar
systemctl --user stop openclaw-gateway

# Reiniciar (tras cambiar config o .md)
systemctl --user restart openclaw-gateway
```

En **Linux (systemd)** el servicio de usuario suele estar en:
- `~/.config/systemd/user/openclaw-gateway.service`
- Habilitar para que arranque al login: `systemctl --user enable openclaw-gateway`

---

## 7. Conectar Telegram

1. Crea un bot con [@BotFather](https://t.me/BotFather), obtén el **token**.
2. En `openclaw.json` añade (o completa) la sección `channels.telegram` como arriba con `botToken`.
3. Reinicia el gateway.
4. Abre un chat con tu bot y envía `/start`. El agente debería responder.

Opcional: restringir quién puede usar el bot con `channels.telegram.allowFrom` (lista de user IDs de Telegram).

---

## 8. Resumen: dejar el agente corriendo solo en un servidor

1. **Instalar** OpenClaw en el servidor (script o npm).
2. **Configurar** con `openclaw setup` y `openclaw onboard --install-daemon` (auth del modelo, gateway, Telegram).
3. **Subir la documentación** del agente (por ejemplo `./deploy-agent-docs.sh` desde este repo).
4. **Asegurar que el servicio está activo**: `systemctl --user enable openclaw-gateway` y `systemctl --user start openclaw-gateway`.
5. **Probar** por Telegram (o `openclaw dashboard` si tienes túnel al puerto 18789).

---

## 9. Comandos útiles

| Comando | Descripción |
|--------|-------------|
| `openclaw doctor` | Diagnóstico de configuración y estado. |
| `openclaw status` | Estado del gateway. |
| `openclaw logs --follow` | Ver logs en vivo. |
| `openclaw dashboard` | Abre la UI en el navegador (acceso al puerto 18789). |
| `openclaw gateway stop` / `start` / `restart` | Control del daemon. |
| `openclaw pairing list telegram` | Ver solicitudes de emparejamiento (si usas pairing). |

---

## 10. Conexión desde tu máquina al Gateway en el servidor

Si el agente corre en un VPS y quieres usar la UI o el CLI desde tu PC:

**Túnel SSH** (puerto 18789):

```bash
ssh -L 18789:localhost:18789 -i TU_CLAVE.pem usuario@tu-servidor
```

Dejar esa sesión abierta y en el navegador ir a `http://127.0.0.1:18789` o ejecutar `openclaw dashboard` en otra terminal (según cómo esté configurado el CLI).

---

## 11. Gateway parado o en restart loop

Si `openclaw status` muestra **Gateway service: stopped (state inactive)** o **unreachable (ECONNREFUSED 127.0.0.1:18789)**:

1. **Arrancar el servicio** (en la instancia, por SSH):
   ```bash
   systemctl --user start openclaw-gateway
   systemctl --user status openclaw-gateway
   ```
2. Si vuelve a caer (**activating (auto-restart)** o **exited, status=1**), ver el error:
   ```bash
   journalctl --user -u openclaw-gateway -n 80 --no-pager
   ```
   o:
   ```bash
   openclaw logs --since "5m ago"
   ```
   Suele ser: config inválida, falta `gateway.auth`, variable de entorno faltante (API key del modelo), o permiso en `~/.openclaw`.
3. **Arreglos frecuentes**:
   - Añadir auth al gateway en `~/.openclaw/openclaw.json`: `"gateway": { "auth": { "mode": "password", "password": "TU_CLAVE" } }`.
   - Permisos: `chmod 700 ~/.openclaw`.
   - Ejecutar `openclaw doctor` y, si sugiere correcciones, `openclaw doctor --fix`.
4. **Canales vacíos**: Si en status la tabla "Channels" está vacía, no hay Telegram/WhatsApp configurado. Añade el canal en `openclaw.json` (p. ej. `channels.telegram.accounts.default.botToken`) y reinicia el gateway.

---

## 12. Referencias

- **Documentación**: [docs.clawd.bot](https://docs.clawd.bot/) (Getting started, Install, Gateway, Channels).
- **Instalación**: [openclaw.ai/install](https://openclaw.ai/install.sh) (script), [Install](https://docs.clawd.bot/install).
- **Configuración**: [Gateway Configuration](https://docs.clawd.bot/gateway/configuration).
- **Este proyecto**: `docs/manual.md` (PumaClaw), `docs/agent/` (archivos .md del agente), `deploy-agent-docs.sh` (despliegue a instancia).

---

### Cómo saber si hay un agente corriendo o instalado

| Qué comprobar | Comando |
|---------------|---------|
| ¿OpenClaw instalado? | `openclaw --version` |
| ¿Gateway corriendo? | `openclaw gateway status` o `openclaw status` |
| ¿Servicio systemd? | `systemctl --user status openclaw-gateway` → **active (running)** = corre; **inactive (dead)** = parado |
| ¿Agentes configurados? | En `openclaw status` ver "Agents" y "Sessions". Revisar `~/.openclaw/openclaw.json` |
| ¿Por qué no arranca? | `journalctl --user -u openclaw-gateway -n 80` o `openclaw logs --since "5m ago"` |

---

*Guía generada para el proyecto claudebot / PumaClaw. Ajusta rutas y nombres según tu instalación.*






1. Entra a tu instancia:

bash
ssh -i "/Users/gerryvela/Documents/AWS/PassPuma.pem" ubuntu@ec2-3-226-236-218.compute-1.amazonaws.com
2. Ya dentro, primero arregla el config roto (quitar la key inválida):

bash
openclaw doctor --fix
3. Limpia procesos fantasma y reinicia:

bash
fuser -k 18789/tcp 2>/dev/null
find ~/.openclaw -name '*.lock' -delete
systemctl --user restart openclaw-gateway
4. Verifica que esté activo:

bash
systemctl --user is-active openclaw-gateway
5. Prueba el script directo para confirmar que funciona:

bash
export $(grep -v '^#' ~/.openclaw/.env | xargs) && ~/.venv/bin/python3 ~/.openclaw/workspace/skills/polymarket/scripts/trade.py list -n 5
El problema de raíz es que gpt-4o-mini prefiere usar web_fetch (búsqueda web) en vez de ejecutar bash para responder preguntas de Polymarket, aunque los docs le dicen que use bash. Si quieres explorar en el wizard interactivo para ver opciones de configuración:

bash
openclaw wizard
O si quieres cambiar a gpt-4.1-mini (que podría seguir mejor las instrucciones y tiene rate limits altos):

bash
openclaw config set agents.defaults.model.primary 'openai/gpt-4.1-mini'
systemctl --user restart openclaw-gateway
¿Quieres que te arme algo más o entras tú directo? 🐆





-----





Commands:
  acp               Agent Control Protocol tools
  agent             Run an agent turn via the Gateway (use --local for embedded)
  agents            Manage isolated agents (workspaces + auth + routing)
  approvals         Exec approvals
  browser           Manage OpenClaw's dedicated browser (Chrome/Chromium)
  channels          Channel management
  completion        Generate shell completion script
  config            Config helpers (get/set/unset). Run without subcommand for the wizard.
  configure         Interactive prompt to set up credentials, devices, and agent defaults
  cron              Cron scheduler
  daemon            Gateway service (legacy alias)
  dashboard         Open the Control UI with your current token
  devices           Device pairing + token management
  directory         Directory commands
  dns               DNS helpers
  docs              Docs helpers
  doctor            Health checks + quick fixes for the gateway and channels
  gateway           Gateway control
  health            Fetch health from the running gateway
  help              display help for command
  hooks             Hooks tooling
  logs              Gateway logs
  memory            Memory search tools
  message           Send messages and channel actions
  models            Model configuration
  node              Node control
  nodes             Node commands
  onboard           Interactive wizard to set up the gateway, workspace, and skills
  pairing           Pairing helpers
  plugins           Plugin management
  reset             Reset local config/state (keeps the CLI installed)
  sandbox           Sandbox tools
  security          Security helpers
  sessions          List stored conversation sessions
  setup             Initialize ~/.openclaw/openclaw.json and the agent workspace
  skills            Skills management
  status            Show channel health and recent session recipients
  system            System events, heartbeat, and presence
  tui               Terminal UI
  uninstall         Uninstall the gateway service + local data (CLI remains)
  update            CLI update helpers
  webhooks          Webhook helpers

Examples:
  openclaw channels login --verbose
    Link personal WhatsApp Web and show QR + connection logs.
  openclaw message send --target +15555550123 --message "Hi" --json
    Send via your web session and print JSON result.
  openclaw gateway --port 18789
    Run the WebSocket Gateway locally.
  openclaw --dev gateway
    Run a dev Gateway (isolated state/config) on ws://127.0.0.1:19001.
  openclaw gateway --force
    Kill anything bound to the default gateway port, then start it.
  openclaw gateway ...
    Gateway control via WebSocket.
  openclaw agent --to +15555550123 --message "Run summary" --deliver
    Talk directly to the agent using the Gateway; optionally send the WhatsApp reply.
  openclaw message send --channel telegram --target @mychat --message "Hi"
    Send via your Telegram bot.

Docs: docs.openclaw.ai/cli