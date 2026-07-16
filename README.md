# ServiceNow Monitor — versión efímera dentro del contenedor

Esta versión no crea carpetas `data`, no usa bind mounts y no declara volúmenes persistentes.

Los tickets conocidos se mantienen solamente en memoria. Los logs, diagnósticos y el archivo de salud se guardan únicamente en la capa interna del contenedor: no se crea ninguna carpeta ni volumen en el host.

## Arranque

Conserva tu archivo `.env` en la raíz del proyecto y ejecuta:

```bash
docker compose up -d --build
```

En una instalación nueva, `docker compose up -d` también construye la imagen si todavía no existe. Se recomienda `--build` después de sustituir el código para garantizar que Docker incorpore los cambios.

No necesitas crear carpetas, ajustar permisos ni montar volúmenes.

## Consultar el estado

```bash
docker ps --filter name=sn-monitor

docker inspect --format='{{.State.Health.Status}}' sn-monitor
```

## Entrar al contenedor

```bash
docker exec -it sn-monitor sh
```

Dentro del contenedor:

```bash
# Log general
tail -f /var/log/sn-monitor/monitor.log

# Errores con traceback
tail -n 250 /var/log/sn-monitor/error.log

# Diagnósticos JSON, screenshots y HTML opcional
ls -lah /var/log/sn-monitor/diagnostics

# Estado del healthcheck
cat /run/sn-monitor/health.json
```

También puedes consultar directamente sin abrir una shell:

```bash
docker exec sn-monitor tail -n 250 /var/log/sn-monitor/error.log

docker exec sn-monitor sh -lc 'ls -lt /var/log/sn-monitor/diagnostics | head -20'
```

## Extraer un diagnóstico solamente cuando lo necesites

Los archivos no salen del contenedor automáticamente. Para copiar uno manualmente:

```bash
docker cp sn-monitor:/var/log/sn-monitor/diagnostics/ARCHIVO.json .
```

## Comportamiento efímero

Al reiniciar o recrear el contenedor:

- En un reinicio normal del mismo contenedor se conservan los logs internos para poder depurar.
- Al eliminar o recrear el contenedor se eliminan los logs y diagnósticos.
- La lista de tickets conocidos siempre se elimina porque solamente vive en memoria.
- La primera consulta crea una línea base en memoria.
- Los tickets que ya existían durante ese arranque no generan notificación.
- Los tickets asignados posteriormente sí generan notificación.

Esto evita duplicados después de un reinicio sin guardar estado fuera del contenedor.

## Recuperación automática

La secuencia predeterminada es:

1. Reintentos internos para errores de red, timeout, HTTP 408, 425, 429 y 5xx.
2. Tras dos fallos de ciclo, vuelve a abrir My To-Do y recaptura payload, headers y token.
3. Tras cuatro fallos, reinicia Chromium y crea una sesión nueva.
4. Tras seis fallos, finaliza el proceso y Docker lo vuelve a iniciar mediante `restart: unless-stopped`.

## Logs y diagnósticos

Rutas internas:

```text
/var/log/sn-monitor/monitor.log
/var/log/sn-monitor/error.log
/var/log/sn-monitor/diagnostics/
/run/sn-monitor/health.json
```

Los logs rotan dentro del contenedor. Los diagnósticos antiguos se eliminan automáticamente cuando superan el número o el tamaño configurado.

Configuración predeterminada:

```env
LOG_MAX_BYTES=5242880
LOG_BACKUP_COUNT=3
DIAGNOSTIC_MAX_FILES=40
DIAGNOSTIC_MAX_TOTAL_BYTES=100663296
```

Las contraseñas, cookies, autorizaciones, webhooks y headers que contienen `token` se redactan en los diagnósticos conocidos. Revisa cualquier JSON antes de compartirlo porque la respuesta de ServiceNow puede contener información de tickets.

## Verificación del código

```bash
python -m unittest discover -s tests -v
```
