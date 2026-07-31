from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    Request,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from discord_notifier import DiscordNotificationError, enviar_ticket_nuevo


# =============================================================================
# CONFIGURACIÓN
# =============================================================================


def env_bool(nombre: str, default: bool) -> bool:
    valor = os.getenv(nombre)
    if valor is None:
        return default
    return valor.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def env_int(nombre: str, default: int, minimo: int | None = None) -> int:
    valor = os.getenv(nombre)
    if valor is None or not valor.strip():
        resultado = default
    else:
        try:
            resultado = int(valor)
        except ValueError as exc:
            raise RuntimeError(f"{nombre} debe ser un número entero.") from exc

    if minimo is not None and resultado < minimo:
        raise RuntimeError(f"{nombre} debe ser al menos {minimo}.")
    return resultado


INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
USERNAME = os.getenv("SERVICENOW_USERNAME", "")
PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")

HEADLESS = env_bool("HEADLESS", True)
IGNORE_HTTPS_ERRORS = env_bool("IGNORE_HTTPS_ERRORS", False)
REFRESH_SECONDS = env_int("REFRESH_SECONDS", 30, 10)
REQUEST_TIMEOUT_SECONDS = env_int("REQUEST_TIMEOUT_SECONDS", 20, 5)
CAPTURE_TIMEOUT_SECONDS = env_int("CAPTURE_TIMEOUT_SECONDS", 90, 20)
SERVICENOW_REQUEST_ATTEMPTS = env_int("SERVICENOW_REQUEST_ATTEMPTS", 3, 1)

RECAPTURE_AFTER_FAILURES = env_int("RECAPTURE_AFTER_FAILURES", 2, 1)
RESTART_BROWSER_AFTER_FAILURES = env_int("RESTART_BROWSER_AFTER_FAILURES", 4, 2)
EXIT_AFTER_FAILURES = env_int("EXIT_AFTER_FAILURES", 6, 3)
MAX_BACKOFF_SECONDS = env_int("MAX_BACKOFF_SECONDS", 60, 1)

LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/sn-monitor"))
DIAGNOSTIC_DIR = Path(
    os.getenv("DIAGNOSTIC_DIR", "/var/log/sn-monitor/diagnostics")
)
HEALTH_FILE = Path(os.getenv("HEALTH_FILE", "/run/sn-monitor/health.json"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = env_int("LOG_MAX_BYTES", 5 * 1024 * 1024, 1024)
LOG_BACKUP_COUNT = env_int("LOG_BACKUP_COUNT", 3, 1)
DIAGNOSTIC_MAX_FILES = env_int("DIAGNOSTIC_MAX_FILES", 40, 1)
DIAGNOSTIC_MAX_TOTAL_BYTES = env_int(
    "DIAGNOSTIC_MAX_TOTAL_BYTES", 96 * 1024 * 1024, 1_048_576
)

SAVE_RESPONSE_BODY_ON_ERROR = env_bool("SAVE_RESPONSE_BODY_ON_ERROR", True)
SAVE_SCREENSHOT_ON_ERROR = env_bool("SAVE_SCREENSHOT_ON_ERROR", True)
SAVE_PAGE_HTML_ON_ERROR = env_bool("SAVE_PAGE_HTML_ON_ERROR", False)
MAX_DIAGNOSTIC_TEXT_CHARS = env_int(
    "MAX_DIAGNOSTIC_TEXT_CHARS", 1_000_000, 2_000
)

SERVICENOW_TABLE = os.getenv("SERVICENOW_TABLE", "task").strip()
SERVICENOW_QUERY_MARKER = os.getenv(
    "SERVICENOW_QUERY_MARKER", "assigned_to"
).strip()
SERVICENOW_PIPELINE_ID = os.getenv(
    "SERVICENOW_PIPELINE_ID", "sn_record_list_composite_broker"
).strip()

LOGIN_URL = f"{INSTANCE}/login.do"
DEFAULT_TODO_URL = (
    f"{INSTANCE}/now/cwf/agent/simplelist/task/params/"
    "list-title/My%20To-Do/query/"
    "assigned_toDYNAMIC90d1921e5f510100a9ad2572f2b477fe"
    "%5EstateNOT%20IN3%2C4%2C7%2C8%2C107%2C157%2C5%2C9%2C21"
)
TODO_URL = os.getenv("SERVICENOW_TODO_URL", DEFAULT_TODO_URL).strip()
EXEC_PATH = "/api/now/uxf/databroker/exec"
EXEC_URL = f"{INSTANCE}{EXEC_PATH}"

CHROMIUM_ARGS = [
    argumento.strip()
    for argumento in os.getenv("CHROMIUM_ARGS", "").split(",")
    if argumento.strip()
]

logger = logging.getLogger("sn_monitor")
LAST_SUCCESS_UTC: str | None = None


# =============================================================================
# EXCEPCIONES Y MODELOS
# =============================================================================


class CaptureError(RuntimeError):
    """No fue posible capturar una consulta válida de My To-Do."""


class SessionExpiredError(RuntimeError):
    """La sesión de ServiceNow expiró o redirigió al login."""


class ServiceNowTransportError(RuntimeError):
    """Fallo transitorio o de transporte al consultar ServiceNow."""


class ServiceNowHTTPError(RuntimeError):
    """ServiceNow devolvió un HTTP no recuperable inmediatamente."""


class UnexpectedResponseError(RuntimeError):
    """ServiceNow respondió, pero el resultado no contiene My To-Do."""


@dataclass
class CaptureResult:
    payload: Any
    headers: dict[str, str]
    response_data: dict[str, Any]


@dataclass
class QueryResult:
    payload: Any
    response_data: dict[str, Any]
    tickets: list[dict[str, str]]


# =============================================================================
# LOGGING, ARCHIVOS Y DIAGNÓSTICO
# =============================================================================


SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "webhook",
    "credential",
    "sysparm_ck",
    "session",
)


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    nivel = getattr(logging, LOG_LEVEL, logging.INFO)
    formato = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(nivel)
    root.handlers.clear()

    consola = logging.StreamHandler(sys.stdout)
    consola.setLevel(nivel)
    consola.setFormatter(formato)
    root.addHandler(consola)

    general = RotatingFileHandler(
        LOG_DIR / "monitor.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    general.setLevel(nivel)
    general.setFormatter(formato)
    root.addHandler(general)

    errores = RotatingFileHandler(
        LOG_DIR / "error.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    errores.setLevel(logging.ERROR)
    errores.setFormatter(formato)
    root.addHandler(errores)


def ahora() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, contenido: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporal = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")

    try:
        with temporal.open("w", encoding="utf-8") as archivo:
            json.dump(contenido, archivo, indent=2, ensure_ascii=False)
            archivo.write("\n")
            archivo.flush()
            os.fsync(archivo.fileno())
        os.replace(temporal, path)
    finally:
        try:
            temporal.unlink(missing_ok=True)
        except OSError:
            pass


def update_health(
    estado: str,
    fallos_consecutivos: int,
    detalle: str = "",
    mark_success: bool = False,
) -> None:
    global LAST_SUCCESS_UTC

    if mark_success:
        LAST_SUCCESS_UTC = utc_iso()

    contenido = {
        "status": estado,
        "updated_at_utc": utc_iso(),
        "last_success_utc": LAST_SUCCESS_UTC,
        "consecutive_failures": fallos_consecutivos,
        "detail": detalle[:2_000],
        "pid": os.getpid(),
    }

    try:
        write_json_atomic(HEALTH_FILE, contenido)
    except Exception:
        logger.exception("No se pudo actualizar el archivo de salud.")


def _es_clave_sensible(clave: str) -> bool:
    normalizada = clave.lower().replace("-", "_")
    return any(parte in normalizada for parte in SENSITIVE_KEY_PARTS)


def redact_sensitive_text(texto: str) -> str:
    if not texto:
        return texto

    patrones = [
        # Webhooks de Discord: el path contiene ID y token secretos.
        (r"https://(?:discord(?:app)?\.com)/api/webhooks/[^\s\"'<>]+",
         "https://discord.com/api/webhooks/***REDACTED***"),
        # Pares JSON o similares con claves sensibles.
        (
            r"(?i)([\"']?(?:password|passwd|token|x-usertoken|x-user-token|"
            r"authorization|cookie|sysparm_ck|session)[\"']?\s*[:=]\s*)"
            r"([\"'])(.*?)(\2)",
            r"\1\2***REDACTED***\4",
        ),
    ]

    resultado = texto
    for patron, reemplazo in patrones:
        resultado = re.sub(patron, reemplazo, resultado)
    return resultado


def sanitize_for_diagnostics(valor: Any, clave: str = "") -> Any:
    if clave and _es_clave_sensible(clave):
        return "***REDACTED***"

    if isinstance(valor, dict):
        return {
            str(k): sanitize_for_diagnostics(v, str(k))
            for k, v in valor.items()
        }

    if isinstance(valor, list):
        return [sanitize_for_diagnostics(item) for item in valor]

    if isinstance(valor, tuple):
        return [sanitize_for_diagnostics(item) for item in valor]

    if isinstance(valor, str) and len(valor) > MAX_DIAGNOSTIC_TEXT_CHARS:
        return (
            valor[:MAX_DIAGNOSTIC_TEXT_CHARS]
            + "\n...[TRUNCADO POR SN-MONITOR]"
        )

    return valor


def diagnostic_name(tipo: str) -> str:
    marca = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    tipo_seguro = "".join(
        caracter if caracter.isalnum() or caracter in {"-", "_"} else "_"
        for caracter in tipo
    )
    return f"{marca}-{tipo_seguro}"


def prune_diagnostics() -> None:
    """Limita diagnósticos efímeros para evitar llenar el tmpfs."""
    try:
        archivos = [
            path for path in DIAGNOSTIC_DIR.iterdir()
            if path.is_file()
        ]
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("No se pudo enumerar el directorio de diagnósticos.")
        return

    try:
        archivos.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        archivos.sort(key=lambda path: path.name, reverse=True)

    conservados: list[Path] = []
    total = 0

    for archivo in archivos:
        try:
            tamano = archivo.stat().st_size
        except OSError:
            tamano = 0

        cabe_por_cantidad = len(conservados) < DIAGNOSTIC_MAX_FILES
        cabe_por_tamano = (total + tamano) <= DIAGNOSTIC_MAX_TOTAL_BYTES

        # Siempre se conserva al menos el archivo más reciente.
        if not conservados or (cabe_por_cantidad and cabe_por_tamano):
            conservados.append(archivo)
            total += tamano
            continue

        try:
            archivo.unlink(missing_ok=True)
        except OSError:
            logger.exception("No se pudo eliminar diagnóstico antiguo: %s", archivo)


def write_diagnostic(
    tipo: str,
    *,
    detalle: dict[str, Any] | None = None,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    response_data: Any = None,
    response_text: str = "",
    page: Page | None = None,
    exception: BaseException | None = None,
) -> list[Path]:
    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    prune_diagnostics()
    base = diagnostic_name(tipo)
    creados: list[Path] = []

    # Si ya existe JSON parseado, la versión estructurada es más útil y puede
    # redactarse por clave. Evitamos duplicarla como texto crudo.
    texto_guardado = "" if response_data is not None else response_text
    texto_guardado = redact_sensitive_text(texto_guardado)

    if not SAVE_RESPONSE_BODY_ON_ERROR:
        texto_guardado = texto_guardado[:2_000]
    elif len(texto_guardado) > MAX_DIAGNOSTIC_TEXT_CHARS:
        texto_guardado = (
            texto_guardado[:MAX_DIAGNOSTIC_TEXT_CHARS]
            + "\n...[TRUNCADO POR SN-MONITOR]"
        )

    documento: dict[str, Any] = {
        "type": tipo,
        "created_at_utc": utc_iso(),
        "detail": detalle or {},
        "payload": payload,
        "headers": headers or {},
        "response_data": response_data,
        "response_text": texto_guardado,
    }

    if exception is not None:
        documento["exception"] = {
            "type": type(exception).__name__,
            "message": str(exception),
        }

    archivo_json = DIAGNOSTIC_DIR / f"{base}.json"
    try:
        write_json_atomic(
            archivo_json,
            sanitize_for_diagnostics(documento),
        )
        creados.append(archivo_json)
    except Exception:
        logger.exception("No se pudo guardar el diagnóstico JSON.")

    if page is not None and SAVE_SCREENSHOT_ON_ERROR:
        captura = DIAGNOSTIC_DIR / f"{base}.png"
        try:
            page.screenshot(path=str(captura), full_page=True, timeout=15_000)
            creados.append(captura)
        except Exception:
            logger.exception("No se pudo guardar la captura de pantalla de debug.")

    if page is not None and SAVE_PAGE_HTML_ON_ERROR:
        html_path = DIAGNOSTIC_DIR / f"{base}.html"
        try:
            html_path.write_text(page.content(), encoding="utf-8")
            creados.append(html_path)
        except Exception:
            logger.exception("No se pudo guardar el HTML de debug.")

    if creados:
        logger.error(
            "Diagnóstico guardado: %s",
            ", ".join(str(path) for path in creados),
        )

    prune_diagnostics()
    return creados


# =============================================================================
# ESTADO EFÍMERO
# =============================================================================

# Los tickets conocidos se conservan exclusivamente en memoria. Al recrear o
# reiniciar el contenedor se genera una línea base nueva y no se notifican como
# nuevos los tickets que ya estaban asignados al arrancar.


# =============================================================================
# IDENTIFICACIÓN DEL PAYLOAD Y EXTRACCIÓN DE TICKETS
# =============================================================================


def es_pagina_login(url: str) -> bool:
    url_normalizada = (url or "").lower()
    return any(
        texto in url_normalizada
        for texto in (
            "login.do",
            "session_timeout.do",
            "logout.do",
        )
    )


def input_value(input_values: dict[str, Any], nombre: str) -> Any:
    valor = input_values.get(nombre)
    if isinstance(valor, dict) and "value" in valor:
        return valor.get("value")
    return valor


def elemento_es_lista_tareas(elemento: Any) -> bool:
    if not isinstance(elemento, dict):
        return False

    if elemento.get("pipelineId") != SERVICENOW_PIPELINE_ID:
        return False

    input_values = elemento.get("inputValues")
    if not isinstance(input_values, dict):
        return False

    tabla = str(input_value(input_values, "table") or "").strip()
    query = str(input_value(input_values, "query") or "").strip()

    if SERVICENOW_TABLE and tabla != SERVICENOW_TABLE:
        return False

    if SERVICENOW_QUERY_MARKER:
        return SERVICENOW_QUERY_MARKER.lower() in query.lower()

    return True


def payload_es_lista_tareas(payload: Any) -> bool:
    elementos = payload if isinstance(payload, list) else [payload]
    return any(elemento_es_lista_tareas(elemento) for elemento in elementos)


def indices_lista_tareas(payload: Any) -> list[int]:
    elementos = payload if isinstance(payload, list) else [payload]
    return [
        indice
        for indice, elemento in enumerate(elementos)
        if elemento_es_lista_tareas(elemento)
    ]


def rows_de_resultado(resultado: Any) -> list[Any] | None:
    if not isinstance(resultado, dict):
        return None

    try:
        rows = (
            resultado["executionResult"]
            ["output"]
            ["rowDefinitions"]
            ["rows"]
        )
    except (KeyError, TypeError):
        return None

    return rows if isinstance(rows, list) else None


def fila_parece_ticket(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    cells = row.get("cells")
    return isinstance(cells, dict) and "number" in cells


def obtener_rows(data: Any, payload: Any) -> list[Any] | None:
    """
    Localiza el resultado asociado al broker My To-Do.

    Primero conserva la correlación por índice payload/result. Si ServiceNow cambia
    el orden, aplica un fallback estructural buscando filas con celda ``number``.
    """
    if not isinstance(data, dict):
        return None

    resultados = data.get("result")
    if isinstance(resultados, dict):
        resultados = [resultados]
    if not isinstance(resultados, list):
        return None

    for indice in indices_lista_tareas(payload):
        if indice >= len(resultados):
            continue
        rows = rows_de_resultado(resultados[indice])
        if rows is not None:
            return rows

    for resultado in resultados:
        rows = rows_de_resultado(resultado)
        if rows is None:
            continue
        if rows and any(fila_parece_ticket(row) for row in rows):
            return rows

    return None


def valor_celda(
    cells: dict[str, Any],
    nombre: str,
    propiedad: str = "value",
) -> str:
    celda = cells.get(nombre, {})
    if not isinstance(celda, dict):
        return ""
    valor = celda.get(propiedad)
    if valor is None and propiedad != "value":
        valor = celda.get("value")
    return str(valor or "").strip()


def extraer_tickets(
    data: dict[str, Any],
    payload: Any,
) -> list[dict[str, str]] | None:
    rows = obtener_rows(data, payload)
    if rows is None:
        return None

    tickets: list[dict[str, str]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        cells = row.get("cells", {})
        if not isinstance(cells, dict):
            continue

        sys_id = str(row.get("key", "") or "").strip()
        if not sys_id:
            sys_id = valor_celda(cells, "sys_id")

        number = valor_celda(cells, "number")
        if not sys_id or not number:
            continue

        number_cell = cells.get("number", {})
        href = ""
        if isinstance(number_cell, dict):
            href = str(number_cell.get("href", "") or "").strip()

        url = urljoin(f"{INSTANCE}/", href) if href else ""

        tickets.append(
            {
                "sys_id": sys_id,
                "number": number,
                "description": valor_celda(cells, "short_description"),
                "priority": valor_celda(cells, "priority", "label"),
                "state": valor_celda(cells, "state", "label"),
                "assigned_to": valor_celda(cells, "assigned_to"),
                "type": valor_celda(cells, "sys_class_name", "label"),
                "href": href,
                "url": url,
            }
        )

    return tickets


def preparar_payload(payload_original: Any) -> Any:
    payload = copy.deepcopy(payload_original)
    elementos = payload if isinstance(payload, list) else [payload]
    request_id = uuid.uuid4().hex
    encontrados = 0
    modificados = 0

    for elemento in elementos:
        if not elemento_es_lista_tareas(elemento):
            continue

        encontrados += 1
        input_values = elemento.get("inputValues")
        if not isinstance(input_values, dict):
            continue

        metadata = input_values.get("requestMetadata")
        if not isinstance(metadata, dict):
            continue

        metadata_value = metadata.get("value")
        if not isinstance(metadata_value, dict):
            continue

        metadata_value.update(
            {
                "requestId": request_id,
                "refreshRequested": True,
                "fromButton": True,
                "appendRows": False,
                "currentPage": 0,
            }
        )
        modificados += 1

    if encontrados == 0:
        raise UnexpectedResponseError(
            "El payload capturado ya no contiene el broker configurado de My To-Do."
        )

    if modificados == 0:
        logger.warning(
            "El broker My To-Do no contiene requestMetadata modificable; "
            "se enviará el payload capturado sin refrescar esa metadata."
        )

    return payload


def seleccionar_headers(headers: dict[str, str]) -> dict[str, str]:
    permitidos = {
        "accept",
        "accept-language",
        "content-type",
        "origin",
        "referer",
        "x-usertoken",
        "x-user-token",
        "now-ui-interaction",
        "now-ux-interaction",
        "x-request-cancelable",
        "x-transaction-source",
    }

    seleccionados: dict[str, str] = {}
    for nombre, valor in headers.items():
        nombre_normalizado = nombre.lower().strip()
        if nombre_normalizado in permitidos:
            seleccionados[nombre_normalizado] = str(valor)

    seleccionados["accept"] = "application/json"
    seleccionados["content-type"] = "application/json"
    return seleccionados


def resumen_respuesta(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"top_level_type": type(data).__name__}

    resultados = data.get("result")
    if isinstance(resultados, dict):
        resultados = [resultados]

    resumen: dict[str, Any] = {
        "top_level_keys": sorted(str(key) for key in data.keys()),
        "result_type": type(data.get("result")).__name__,
    }

    if isinstance(resultados, list):
        resumen["result_count"] = len(resultados)
        resumen["results"] = []
        for indice, resultado in enumerate(resultados[:20]):
            item = {
                "index": indice,
                "type": type(resultado).__name__,
                "keys": (
                    sorted(str(key) for key in resultado.keys())
                    if isinstance(resultado, dict)
                    else []
                ),
            }
            rows = rows_de_resultado(resultado)
            item["has_rows_path"] = rows is not None
            item["row_count"] = len(rows) if rows is not None else None
            resumen["results"].append(item)

    return resumen


# =============================================================================
# SALIDA Y NOTIFICACIONES
# =============================================================================


def imprimir_lista_inicial(tickets: list[dict[str, str]]) -> None:
    logger.info("=" * 100)
    logger.info("TICKETS ACTUALES: %s", len(tickets))
    logger.info("=" * 100)

    if not tickets:
        logger.info("No hay tickets actualmente asignados.")
        return

    for ticket in tickets:
        logger.info(
            "%s | %s | %s | %s | %s",
            ticket["number"],
            ticket["priority"] or "Sin prioridad",
            ticket["state"] or "Sin estado",
            ticket["type"] or "Sin tipo",
            ticket["description"] or "Sin descripción",
        )
        if ticket["url"]:
            logger.info("URL: %s", ticket["url"])


def imprimir_ticket_nuevo(ticket: dict[str, str]) -> None:
    logger.warning("!" * 100)
    logger.warning("NUEVO TICKET ASIGNADO")
    logger.warning("Número: %s", ticket["number"])
    logger.warning("Tipo: %s", ticket["type"] or "Sin tipo")
    logger.warning("Prioridad: %s", ticket["priority"] or "Sin prioridad")
    logger.warning("Estado: %s", ticket["state"] or "Sin estado")
    logger.warning("Asignado a: %s", ticket["assigned_to"] or "Sin asignar")
    logger.warning("Descripción: %s", ticket["description"] or "Sin descripción")
    if ticket["url"]:
        logger.warning("URL: %s", ticket["url"])
    logger.warning("!" * 100)


def notificar_discord(ticket: dict[str, str]) -> bool:
    try:
        enviar_ticket_nuevo(ticket)
        logger.info(
            "Notificación enviada a Discord: %s",
            ticket["number"],
        )
        return True
    except DiscordNotificationError as exc:
        logger.error(
            "No se pudo notificar a Discord el ticket %s: %s",
            ticket["number"],
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "Error inesperado notificando Discord para %s.",
            ticket["number"],
        )
        return False


class TicketTracker:
    def __init__(self) -> None:
        self.known_ids: set[str] = set()
        self.needs_baseline = True
        self.initial_list_shown = False

    def process(
        self,
        tickets: list[dict[str, str]],
        *,
        show_initial_list: bool = False,
    ) -> None:
        actuales = {ticket["sys_id"] for ticket in tickets}
        logger.info("Consulta correcta: %s tickets.", len(tickets))

        if show_initial_list and not self.initial_list_shown:
            imprimir_lista_inicial(tickets)
            self.initial_list_shown = True

        if self.needs_baseline:
            self.known_ids = actuales
            self.needs_baseline = False
            logger.warning(
                "Línea base creada. Los tickets actuales no se notifican."
            )
            return

        # Se eliminan del estado los tickets que ya no están asignados. Si vuelven
        # posteriormente, se considerarán una asignación nueva.
        confirmados = self.known_ids & actuales
        nuevos_ids = {
            ticket["sys_id"]
            for ticket in tickets
            if ticket["sys_id"] not in confirmados
        }

        if nuevos_ids:
            logger.warning("Se detectaron %s tickets nuevos.", len(nuevos_ids))

        for ticket in tickets:
            sys_id = ticket["sys_id"]
            if sys_id in confirmados:
                continue

            imprimir_ticket_nuevo(ticket)

            if not notificar_discord(ticket):
                # No se marca como conocido. Se volverá a intentar en el próximo ciclo.
                continue

            confirmados.add(sys_id)
            self.known_ids = confirmados

        self.known_ids = confirmados


# =============================================================================
# NAVEGADOR, LOGIN Y CAPTURA
# =============================================================================


def navegador_host_valido() -> bool:
    if not INSTANCE:
        return False
    parsed = urlparse(INSTANCE)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def navegar_seguro(page: Page, url: str, esperar: str = "commit") -> None:
    try:
        page.goto(url, wait_until=esperar, timeout=120_000)
    except PlaywrightError as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
        logger.warning(
            "ServiceNow abortó la navegación externa, pero se continuará "
            "esperando al workspace."
        )
        page.wait_for_timeout(3_000)


def request_payload(request: Request) -> Any:
    try:
        return request.post_data_json
    except Exception:
        return None


def request_headers(request: Request) -> dict[str, str]:
    try:
        return request.all_headers()
    except Exception:
        return dict(request.headers)


class ServiceNowRuntime:
    def __init__(self, playwright: Playwright) -> None:
        self.playwright = playwright
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.capture: CaptureResult | None = None

    def start(self) -> None:
        if self.browser is None or not self.browser.is_connected():
            logger.info("Iniciando Chromium...")
            self.browser = self.playwright.chromium.launch(
                headless=HEADLESS,
                args=CHROMIUM_ARGS,
            )

        self._new_context()

    def _new_context(self) -> None:
        if self.browser is None:
            raise RuntimeError("Chromium no está iniciado.")

        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                logger.exception("Error cerrando el contexto anterior de Chromium.")

        self.context = self.browser.new_context(
            locale="es-MX",
            timezone_id="America/Mexico_City",
            ignore_https_errors=IGNORE_HTTPS_ERRORS,
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(30_000)
        self.page.set_default_navigation_timeout(120_000)
        self.capture = None
        self._attach_observability(self.page)

    def _attach_observability(self, page: Page) -> None:
        def on_page_error(error: Any) -> None:
            logger.error("Error JavaScript de página: %s", error)

        def on_console(message: Any) -> None:
            try:
                if message.type in {"error", "warning"}:
                    logger.debug(
                        "Consola ServiceNow [%s]: %s",
                        message.type,
                        message.text,
                    )
            except Exception:
                pass

        page.on("pageerror", on_page_error)
        page.on("console", on_console)

    def restart_browser(self) -> None:
        logger.warning("Reiniciando completamente Chromium y su sesión...")
        self.close()
        time.sleep(1)
        self.start()

    def close(self) -> None:
        self.capture = None

        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                logger.exception("Error cerrando el contexto de Chromium.")
            self.context = None
            self.page = None

        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                logger.exception("Error cerrando Chromium.")
            self.browser = None

    def _require_page(self) -> Page:
        if self.page is None or self.page.is_closed():
            raise RuntimeError("La página de Chromium no está disponible.")
        return self.page

    def _require_context(self) -> BrowserContext:
        if self.context is None:
            raise RuntimeError("El contexto de Chromium no está disponible.")
        return self.context

    def iniciar_sesion(self) -> None:
        page = self._require_page()
        logger.info("Iniciando sesión en ServiceNow...")
        navegar_seguro(page, LOGIN_URL, esperar="domcontentloaded")

        # Una sesión todavía válida puede redirigir inmediatamente fuera de login.do.
        if not es_pagina_login(page.url):
            logger.info("ServiceNow conservó una sesión autenticada.")
            return

        try:
            page.locator("#user_name").wait_for(state="visible", timeout=30_000)
        except PlaywrightTimeoutError as exc:
            write_diagnostic(
                "login-form-not-found",
                detalle={"url": page.url},
                page=page,
                exception=exc,
            )
            raise RuntimeError(
                "No apareció el formulario estándar de login. "
                "La instancia puede requerir SSO o MFA."
            ) from exc

        page.locator("#user_name").fill(USERNAME)
        page.locator("#user_password").fill(PASSWORD)
        page.locator("#sysverb_login").click()

        try:
            page.wait_for_function(
                """
                () => {
                    const url = location.href.toLowerCase();
                    return !url.includes('login.do') &&
                           !url.includes('session_timeout.do') &&
                           !url.includes('logout.do');
                }
                """,
                timeout=60_000,
            )
        except PlaywrightTimeoutError as exc:
            mensaje = ""
            try:
                mensaje = page.locator("#output_messages").inner_text().strip()
            except Exception:
                pass

            write_diagnostic(
                "login-timeout",
                detalle={"url": page.url, "message": mensaje},
                page=page,
                exception=exc,
            )

            if mensaje:
                raise RuntimeError(
                    f"ServiceNow rechazó el login: {mensaje}"
                ) from exc

            raise RuntimeError(
                "No fue posible iniciar sesión. Revisa credenciales, MFA o SSO."
            ) from exc

        logger.info("Sesión iniciada correctamente.")

    def capture_my_todo(self) -> CaptureResult:
        page = self._require_page()
        state: dict[str, Any] = {
            "capture": None,
            "last_candidate": None,
            "last_request_failure": None,
        }

        def on_response(response: Response) -> None:
            try:
                request = response.request
                if EXEC_PATH not in request.url:
                    return
                if request.method.upper() != "POST":
                    return

                payload = request_payload(request)
                if payload is None or not payload_es_lista_tareas(payload):
                    return

                headers = seleccionar_headers(request_headers(request))
                text = response.text()
                data: Any = None

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    pass

                candidate = {
                    "status": response.status,
                    "url": response.url,
                    "content_type": response.headers.get("content-type", ""),
                    "payload": payload,
                    "headers": headers,
                    "text": text,
                    "data": data,
                }
                state["last_candidate"] = candidate

                if response.status != 200 or not isinstance(data, dict):
                    return

                if obtener_rows(data, payload) is None:
                    return

                state["capture"] = CaptureResult(
                    payload=copy.deepcopy(payload),
                    headers=headers,
                    response_data=data,
                )
                logger.info("Petición y respuesta válidas de My To-Do capturadas.")

            except Exception as exc:
                logger.exception("Error procesando una respuesta durante la captura.")
                state["last_candidate"] = {
                    "callback_error": str(exc),
                }

        def on_request_failed(request: Request) -> None:
            if EXEC_PATH not in request.url:
                return
            if request.method.upper() != "POST":
                return

            failure = request.failure
            state["last_request_failure"] = {
                "url": request.url,
                "method": request.method,
                "failure": failure,
            }
            logger.error(
                "Petición UI de ServiceNow fallida: %s | %s",
                request.url,
                failure,
            )

        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

        try:
            logger.info("Abriendo My To-Do...")
            navegar_seguro(page, TODO_URL, esperar="domcontentloaded")

            if es_pagina_login(page.url):
                self.iniciar_sesion()
                navegar_seguro(page, TODO_URL, esperar="domcontentloaded")
                
            # -- INICIO PAUSA DE ESTABILIZACIÓN --
            logger.info("Esperando estabilización del frontend para evitar abortos (net::ERR_ABORTED)...")
            try:
                # Damos tiempo a que los macroponents hidraten
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception as e:
                logger.debug("La red no se estabilizó del todo, pero continuamos (timeout): %s", e)
            # -- FIN PAUSA DE ESTABILIZACIÓN --

            logger.info("Esperando el Data Broker de My To-Do. URL: %s", page.url)
            limite = time.monotonic() + CAPTURE_TIMEOUT_SECONDS

            while time.monotonic() < limite:
                captura = state.get("capture")
                if isinstance(captura, CaptureResult):
                    self.capture = captura
                    return captura

                if es_pagina_login(page.url):
                    raise SessionExpiredError(
                        "ServiceNow redirigió al login durante la captura."
                    )

                page.wait_for_timeout(250)

            candidate = state.get("last_candidate") or {}
            write_diagnostic(
                "capture-timeout",
                detalle={
                    "page_url": page.url,
                    "last_request_failure": state.get("last_request_failure"),
                    "candidate_status": candidate.get("status"),
                    "candidate_url": candidate.get("url"),
                    "candidate_content_type": candidate.get("content_type"),
                    "response_summary": resumen_respuesta(candidate.get("data")),
                },
                payload=candidate.get("payload"),
                headers=candidate.get("headers"),
                response_data=candidate.get("data"),
                response_text=candidate.get("text", ""),
                page=page,
            )

            if candidate:
                raise CaptureError(
                    "Se capturó una petición candidata, pero su respuesta no "
                    "contenía la estructura válida de My To-Do."
                )

            raise CaptureError(
                "No se capturó ninguna petición de My To-Do dentro del timeout."
            )

        finally:
            try:
                page.remove_listener("response", on_response)
                page.remove_listener("requestfailed", on_request_failed)
            except Exception:
                logger.exception("No se pudieron retirar los listeners de captura.")

    def query_my_todo(self) -> QueryResult:
        context = self._require_context()
        page = self._require_page()

        if self.capture is None:
            raise CaptureError("No existe un payload capturado.")

        last_error: BaseException | None = None

        for intento in range(1, SERVICENOW_REQUEST_ATTEMPTS + 1):
            payload = preparar_payload(self.capture.payload)
            response = None
            text = ""
            response_data: Any = None
            response_headers: dict[str, str] = {}
            status = 0
            final_url = ""

            try:
                response = context.request.post(
                    EXEC_URL,
                    headers=self.capture.headers,
                    data=json.dumps(payload, ensure_ascii=False),
                    timeout=REQUEST_TIMEOUT_SECONDS * 1_000,
                    fail_on_status_code=False,
                    max_retries=1,
                )

                status = response.status
                final_url = response.url
                response_headers = dict(response.headers)
                text = response.text()

                logger.debug(
                    "ServiceNow HTTP %s | intento %s/%s | %s bytes | %s",
                    status,
                    intento,
                    SERVICENOW_REQUEST_ATTEMPTS,
                    len(text.encode("utf-8", errors="replace")),
                    final_url,
                )

                content_type = response_headers.get("content-type", "").lower()
                final_url_lower = final_url.lower()

                if status in {408, 425, 429} or 500 <= status <= 599:
                    mensaje = f"ServiceNow devolvió HTTP transitorio {status}."
                    last_error = ServiceNowTransportError(mensaje)

                    if intento < SERVICENOW_REQUEST_ATTEMPTS:
                        espera = retry_delay(
                            intento,
                            response_headers,
                            text,
                        )
                        logger.warning("%s Reintento en %.1f segundos.", mensaje, espera)
                        time.sleep(espera)
                        continue

                    write_diagnostic(
                        "servicenow-http-transient",
                        detalle={
                            "attempt": intento,
                            "status": status,
                            "final_url": final_url,
                            "content_type": content_type,
                        },
                        payload=payload,
                        headers=self.capture.headers,
                        response_text=text,
                        page=page,
                        exception=last_error,
                    )
                    raise last_error

                if (
                    status in {401, 403}
                    or es_pagina_login(final_url_lower)
                    or (status == 200 and "text/html" in content_type)
                ):
                    error = SessionExpiredError(
                        f"La sesión de ServiceNow expiró. HTTP {status}."
                    )
                    write_diagnostic(
                        "servicenow-session-expired",
                        detalle={
                            "attempt": intento,
                            "status": status,
                            "final_url": final_url,
                            "content_type": content_type,
                        },
                        payload=payload,
                        headers=self.capture.headers,
                        response_text=text,
                        page=page,
                        exception=error,
                    )
                    raise error

                if status != 200:
                    error = ServiceNowHTTPError(
                        f"Data Broker devolvió HTTP {status}."
                    )
                    write_diagnostic(
                        "servicenow-http-error",
                        detalle={
                            "attempt": intento,
                            "status": status,
                            "final_url": final_url,
                            "content_type": content_type,
                        },
                        payload=payload,
                        headers=self.capture.headers,
                        response_text=text,
                        page=page,
                        exception=error,
                    )
                    raise error

                try:
                    response_data = json.loads(text)
                except json.JSONDecodeError as exc:
                    error = UnexpectedResponseError(
                        "ServiceNow respondió HTTP 200, pero el body no era JSON."
                    )
                    write_diagnostic(
                        "servicenow-invalid-json",
                        detalle={
                            "attempt": intento,
                            "status": status,
                            "final_url": final_url,
                            "content_type": content_type,
                        },
                        payload=payload,
                        headers=self.capture.headers,
                        response_text=text,
                        page=page,
                        exception=exc,
                    )
                    raise error from exc

                if not isinstance(response_data, dict):
                    error = UnexpectedResponseError(
                        "El JSON de ServiceNow no es un objeto."
                    )
                    write_diagnostic(
                        "servicenow-json-shape",
                        detalle={
                            "summary": resumen_respuesta(response_data),
                        },
                        payload=payload,
                        headers=self.capture.headers,
                        response_data=response_data,
                        page=page,
                        exception=error,
                    )
                    raise error

                tickets = extraer_tickets(response_data, payload)
                if tickets is None:
                    error = UnexpectedResponseError(
                        "La respuesta no contenía el resultado correlacionado de My To-Do."
                    )
                    write_diagnostic(
                        "servicenow-unexpected-structure",
                        detalle={
                            "attempt": intento,
                            "status": status,
                            "final_url": final_url,
                            "response_summary": resumen_respuesta(response_data),
                            "payload_my_todo_indices": indices_lista_tareas(payload),
                        },
                        payload=payload,
                        headers=self.capture.headers,
                        response_data=response_data,
                        response_text=text,
                        page=page,
                        exception=error,
                    )
                    raise error

                return QueryResult(
                    payload=payload,
                    response_data=response_data,
                    tickets=tickets,
                )

            except (SessionExpiredError, ServiceNowHTTPError, UnexpectedResponseError):
                raise
            except PlaywrightTimeoutError as exc:
                last_error = ServiceNowTransportError(
                    f"Timeout de {REQUEST_TIMEOUT_SECONDS}s consultando ServiceNow."
                )
                if intento < SERVICENOW_REQUEST_ATTEMPTS:
                    espera = exponential_delay(intento)
                    logger.warning("%s Reintento en %.1f segundos.", last_error, espera)
                    time.sleep(espera)
                    continue

                write_diagnostic(
                    "servicenow-request-timeout",
                    detalle={"attempt": intento, "final_url": final_url},
                    payload=payload,
                    headers=self.capture.headers,
                    response_text=text,
                    page=page,
                    exception=exc,
                )
                raise last_error from exc

            except PlaywrightError as exc:
                last_error = ServiceNowTransportError(
                    f"Fallo de transporte de Playwright: {exc}"
                )
                
                # Si perdimos el driver, los retries internos no servirán. Salimos de inmediato.
                if "Connection closed" in str(exc) or "browser has been closed" in str(exc):
                    logger.error("Error crítico de Playwright detectado. Abortando retries internos.")
                    raise last_error from exc
                    
                if intento < SERVICENOW_REQUEST_ATTEMPTS:
                    espera = exponential_delay(intento)
                    logger.warning("%s Reintento en %.1f segundos.", last_error, espera)
                    time.sleep(espera)
                    continue

                write_diagnostic(
                    "servicenow-request-transport",
                    detalle={"attempt": intento, "final_url": final_url},
                    payload=payload,
                    headers=self.capture.headers,
                    response_text=text,
                    page=page,
                    exception=exc,
                )
                raise last_error from exc

            finally:
                if response is not None:
                    try:
                        response.dispose()
                    except Exception:
                        logger.debug("No se pudo liberar APIResponse.", exc_info=True)

        if last_error is not None:
            raise last_error
        raise ServiceNowTransportError("La consulta terminó sin respuesta ni excepción.")


def exponential_delay(intento: int) -> float:
    return float(min(MAX_BACKOFF_SECONDS, max(1, 2 ** (intento - 1))))


def retry_delay(
    intento: int,
    headers: dict[str, str],
    body: str,
) -> float:
    retry_after = headers.get("retry-after", "").strip()
    if retry_after:
        try:
            return min(MAX_BACKOFF_SECONDS, max(0.5, float(retry_after)))
        except ValueError:
            pass

    try:
        data = json.loads(body)
        if isinstance(data, dict) and "retry_after" in data:
            return min(
                MAX_BACKOFF_SECONDS,
                max(0.5, float(data["retry_after"])),
            )
    except (ValueError, TypeError, json.JSONDecodeError):
        pass

    return exponential_delay(intento)


# =============================================================================
# VALIDACIÓN Y MAIN
# =============================================================================


def validar_configuracion() -> None:
    faltantes = [
        nombre
        for nombre, valor in {
            "SERVICENOW_INSTANCE": INSTANCE,
            "SERVICENOW_USERNAME": USERNAME,
            "SERVICENOW_PASSWORD": PASSWORD,
        }.items()
        if not valor
    ]

    if faltantes:
        raise RuntimeError(
            "Faltan variables obligatorias: " + ", ".join(faltantes)
        )

    if not navegador_host_valido():
        raise RuntimeError(
            "SERVICENOW_INSTANCE debe ser una URL http/https válida."
        )

    if not TODO_URL.startswith(("http://", "https://")):
        raise RuntimeError("SERVICENOW_TODO_URL debe ser una URL completa.")

    if not (
        RECAPTURE_AFTER_FAILURES
        < RESTART_BROWSER_AFTER_FAILURES
        < EXIT_AFTER_FAILURES
    ):
        raise RuntimeError(
            "Los umbrales deben cumplir: RECAPTURE_AFTER_FAILURES < "
            "RESTART_BROWSER_AFTER_FAILURES < EXIT_AFTER_FAILURES."
        )


def recapturar_y_procesar(
    runtime: ServiceNowRuntime,
    tracker: TicketTracker,
    *,
    restart_browser: bool,
) -> None:
    if restart_browser:
        runtime.restart_browser()

    captura = runtime.capture_my_todo()
    tickets = extraer_tickets(captura.response_data, captura.payload)
    if tickets is None:
        raise UnexpectedResponseError(
            "La captura se completó, pero no pudo extraerse My To-Do."
        )
    tracker.process(tickets)


def run_monitor() -> None:
    validar_configuracion()

    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)

    tracker = TicketTracker()
    fallos_consecutivos = 0

    update_health("starting", 0, "Inicializando Chromium y ServiceNow.")

    with sync_playwright() as playwright:
        runtime = ServiceNowRuntime(playwright)

        try:
            runtime.start()
            captura = runtime.capture_my_todo()
            tickets_iniciales = extraer_tickets(
                captura.response_data,
                captura.payload,
            )

            if tickets_iniciales is None:
                raise UnexpectedResponseError(
                    "La respuesta inicial no contiene la lista My To-Do."
                )

            tracker.process(tickets_iniciales, show_initial_list=True)
            update_health(
                "healthy",
                0,
                f"Consulta inicial correcta: {len(tickets_iniciales)} tickets.",
                mark_success=True,
            )

            logger.info("Monitor activo. Consulta cada %s segundos.", REFRESH_SECONDS)
            logger.info("Log general: %s", LOG_DIR / "monitor.log")
            logger.info("Log de errores: %s", LOG_DIR / "error.log")
            logger.info("Diagnósticos efímeros: %s", DIAGNOSTIC_DIR)
            logger.info("Estado de tickets: solo memoria; se reinicia con el contenedor.")

            while True:
                time.sleep(REFRESH_SECONDS)

                try:
                    resultado = runtime.query_my_todo()
                    tracker.process(resultado.tickets)
                    fallos_consecutivos = 0
                    update_health(
                        "healthy",
                        0,
                        f"Consulta correcta: {len(resultado.tickets)} tickets.",
                        mark_success=True,
                    )

                except KeyboardInterrupt:
                    raise

                except Exception as exc:
                    fallos_consecutivos += 1
                    logger.exception(
                        "Fallo %s/%s en el ciclo de ServiceNow: %s",
                        fallos_consecutivos,
                        EXIT_AFTER_FAILURES,
                        exc,
                    )
                    update_health(
                        "degraded",
                        fallos_consecutivos,
                        f"{type(exc).__name__}: {exc}",
                    )

                    str_exc = str(exc)
                    es_error_critico_driver = "Connection closed" in str_exc or "browser has been closed" in str_exc

                    should_recapture = (
                        isinstance(exc, SessionExpiredError)
                        or fallos_consecutivos >= RECAPTURE_AFTER_FAILURES
                        or es_error_critico_driver
                    )

                    if should_recapture:
                        restart = (
                            fallos_consecutivos >= RESTART_BROWSER_AFTER_FAILURES
                            or es_error_critico_driver
                        )
                        try:
                            logger.warning(
                                "Intentando recuperación: %s.",
                                "reinicio completo de Chromium"
                                if restart
                                else "recaptura de My To-Do",
                            )
                            recapturar_y_procesar(
                                runtime,
                                tracker,
                                restart_browser=restart,
                            )
                            fallos_consecutivos = 0
                            update_health(
                                "healthy",
                                0,
                                "Recuperación automática completada.",
                                mark_success=True,
                            )
                            logger.warning("Recuperación automática completada.")
                            continue

                        except Exception as recovery_exc:
                            fallos_consecutivos += 1
                            logger.exception(
                                "Falló la recuperación automática: %s",
                                recovery_exc,
                            )
                            page = runtime.page
                            write_diagnostic(
                                "automatic-recovery-failed",
                                detalle={
                                    "consecutive_failures": fallos_consecutivos,
                                    "original_error": (
                                        f"{type(exc).__name__}: {exc}"
                                    ),
                                    "recovery_error": (
                                        f"{type(recovery_exc).__name__}: "
                                        f"{recovery_exc}"
                                    ),
                                },
                                page=page,
                                exception=recovery_exc,
                            )
                            update_health(
                                "degraded",
                                fallos_consecutivos,
                                f"Recovery failed: {recovery_exc}",
                            )

                    if fallos_consecutivos >= EXIT_AFTER_FAILURES:
                        raise RuntimeError(
                            "Se alcanzó el máximo de fallos consecutivos. "
                            "El proceso terminará para que Docker lo reinicie."
                        ) from exc

                    espera = min(
                        MAX_BACKOFF_SECONDS,
                        max(1, 2 ** min(fallos_consecutivos, 6)),
                    )
                    logger.warning(
                        "El siguiente intento se realizará en %.1f segundos.",
                        espera,
                    )
                    time.sleep(espera)

        finally:
            runtime.close()


def main() -> int:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        configure_logging()
        run_monitor()
        return 0
    except KeyboardInterrupt:
        logger.info("Monitor detenido por el usuario.")
        update_health("stopped", 0, "Detenido por el usuario.")
        return 0
    except Exception as exc:
        # configure_logging puede fallar si el filesystem interno no está disponible.
        if logging.getLogger().handlers:
            logger.exception("El monitor terminó por un error fatal: %s", exc)
            update_health("fatal", EXIT_AFTER_FAILURES, str(exc))
        else:
            print(f"ERROR FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
