# ServiceNow Monitor

Este proyecto monitorea una instancia de ServiceNow y envía una notificación a un canal de Discord cuando detecta un ticket nuevo.

## Requisitos

* Docker
* Docker Compose

Puedes verificar que estén instalados con:

```bash
docker --version
docker compose version
```

## Estructura del proyecto

```text
service-now-docker/
├── Dockerfile
├── compose.yaml
├── monitor.py
├── discord_notifier.py
├── requirements.txt
├── README.md
├── .env
└── data/
```

## Configuración

Antes de iniciar el contenedor es necesario crear el archivo `.env` con la configuración de ServiceNow y Discord.

### ServiceNow

Completa los datos de tu instancia y del usuario que utilizará el monitor.

```env
SERVICENOW_INSTANCE=https://tu-instancia.service-now.com
SERVICENOW_USERNAME=usuario
SERVICENOW_PASSWORD=contraseña
```

### Discord

1. Crea un servidor (si aún no tienes uno).
2. Crea un canal para recibir las notificaciones.
3. En el canal entra a:

```
Editar canal
    └── Integraciones
          └── Webhooks
                └── Nuevo Webhook
```

4. Asigna un nombre al Webhook y copia la URL generada.
5. Agrega esa URL al archivo `.env`.

```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxxxxxxxxxxxxx
DISCORD_USERNAME=ServiceNow Monitor
```

## Archivo .env

El archivo queda con una estructura similar a la siguiente:

```env
SERVICENOW_INSTANCE=https://tu-instancia.service-now.com
SERVICENOW_USERNAME=usuario
SERVICENOW_PASSWORD=contraseña

DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxxxxxxxxxxxxx
DISCORD_USERNAME=ServiceNow Monitor

HEADLESS=true
REFRESH_SECONDS=30
REQUEST_TIMEOUT_SECONDS=20
STATE_FILE=/app/data/tickets_conocidos.json
```

### Descripción de las variables

| Variable                  | Descripción                                                     |
| ------------------------- | --------------------------------------------------------------- |
| `SERVICENOW_INSTANCE`     | URL de la instancia de ServiceNow.                              |
| `SERVICENOW_USERNAME`     | Usuario utilizado para iniciar sesión en ServiceNow.            |
| `SERVICENOW_PASSWORD`     | Contraseña del usuario.                                         |
| `DISCORD_WEBHOOK_URL`     | URL del Webhook del canal de Discord.                           |
| `DISCORD_USERNAME`        | Nombre que aparecerá en las notificaciones de Discord.          |
| `HEADLESS`                | Ejecuta Chromium sin interfaz gráfica.                          |
| `REFRESH_SECONDS`         | Tiempo entre cada consulta a ServiceNow.                        |
| `REQUEST_TIMEOUT_SECONDS` | Tiempo máximo de espera para cada consulta.                     |
| `STATE_FILE`              | Archivo donde se guarda el estado de los tickets ya procesados. |

## Construcción y ejecución

Para construir la imagen y levantar el contenedor ejecuta:

```bash
docker compose up -d --build
```

La primera vez puede tardar algunos minutos porque se descargan e instalan las dependencias necesarias, incluyendo Chromium.
