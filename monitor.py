from __future__ import annotations

import copy
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    Request,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from discord_notifier import (
    DiscordNotificationError,
    enviar_ticket_nuevo,
)


# ==========================================================
# CONFIGURACIÓN
# ==========================================================

INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
USERNAME = os.getenv("SERVICENOW_USERNAME", "")
PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")

# En Docker debe permanecer en true.
HEADLESS = os.getenv("HEADLESS", "true").lower() in {"1", "true", "yes", "si"}
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))
STATE_FILE = Path(os.getenv("STATE_FILE", "/app/data/tickets_conocidos.json"))

LOGIN_URL = f"{INSTANCE}/login.do"

TODO_URL = (
    f"{INSTANCE}/now/cwf/agent/simplelist/task/params/"
    "list-title/My%20To-Do/query/"
    "assigned_toDYNAMIC90d1921e5f510100a9ad2572f2b477fe"
    "%5EstateNOT%20IN3%2C4%2C7%2C8%2C107%2C157%2C5%2C9%2C21"
)

EXEC_PATH = "/api/now/uxf/databroker/exec"
EXEC_URL = f"{INSTANCE}{EXEC_PATH}"


# ==========================================================
# ESTADO EN MEMORIA
# ==========================================================

payload_exec: Any = None
headers_exec: dict[str, str] = {}
respuesta_inicial: dict[str, Any] | None = None

tickets_conocidos: set[str] = set()
lista_inicial_mostrada = False


# ==========================================================
# UTILIDADES
# ==========================================================

def ahora() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def es_pagina_login(url: str) -> bool:
    url = url.lower()

    return any(
        texto in url
        for texto in (
            "login.do",
            "session_timeout.do",
            "logout.do",
        )
    )


def valor_celda(
    cells: dict[str, Any],
    nombre: str,
    propiedad: str = "value",
) -> str:
    celda = cells.get(nombre, {})

    if not isinstance(celda, dict):
        return ""

    valor = celda.get(propiedad)

    if valor is None:
        return ""

    return str(valor).strip()


# ==========================================================
# EXTRACCIÓN DE TICKETS
# ==========================================================

def obtener_rows(data: Any) -> list[Any] | None:
    """
    Devuelve:
    - lista de filas si la respuesta corresponde a My To-Do;
    - None si es otra petición al mismo endpoint.
    """

    try:
        rows = (
            data["result"][0]
            ["executionResult"]
            ["output"]
            ["rowDefinitions"]
            ["rows"]
        )
    except (KeyError, IndexError, TypeError):
        return None

    if not isinstance(rows, list):
        return None

    return rows


def extraer_tickets(
    data: dict[str, Any],
) -> list[dict[str, str]] | None:
    rows = obtener_rows(data)

    if rows is None:
        return None

    tickets: list[dict[str, str]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        cells = row.get("cells", {})

        if not isinstance(cells, dict):
            continue

        sys_id = str(row.get("key", "")).strip()
        number = valor_celda(cells, "number")

        if not sys_id or not number:
            continue

        number_cell = cells.get("number", {})
        href = ""

        if isinstance(number_cell, dict):
            href = str(
                number_cell.get("href", "") or ""
            ).strip()

        url = ""

        if href:
            url = f"{INSTANCE}{href}"

        tickets.append(
            {
                "sys_id": sys_id,
                "number": number,
                "description": valor_celda(
                    cells,
                    "short_description",
                ),
                "priority": valor_celda(
                    cells,
                    "priority",
                    "label",
                ),
                "state": valor_celda(
                    cells,
                    "state",
                    "label",
                ),
                "assigned_to": valor_celda(
                    cells,
                    "assigned_to",
                ),
                "type": valor_celda(
                    cells,
                    "sys_class_name",
                    "label",
                ),
                "href": href,
                "url": url,
            }
        )

    return tickets


# ==========================================================
# ESTADO LOCAL
# ==========================================================

def cargar_tickets_conocidos() -> set[str]:
    if not STATE_FILE.exists():
        return set()

    try:
        contenido = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return set()

    if not isinstance(contenido, list):
        return set()

    return {
        str(item).strip()
        for item in contenido
        if str(item).strip()
    }


def guardar_tickets_conocidos(sys_ids: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps(
            sorted(sys_ids),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ==========================================================
# SALIDA EN TERMINAL
# ==========================================================

def imprimir_lista_inicial(
    tickets: list[dict[str, str]],
) -> None:
    print()
    print("=" * 120)
    print(f"TICKETS ACTUALES: {len(tickets)}")
    print("=" * 120)

    if not tickets:
        print("No hay tickets actualmente asignados.")
        print("=" * 120)
        print()
        return

    for ticket in tickets:
        print(
            f"{ticket['number']} | "
            f"{ticket['priority'] or 'Sin prioridad'} | "
            f"{ticket['state'] or 'Sin estado'} | "
            f"{ticket['type'] or 'Sin tipo'}"
        )

        print(
            f"  "
            f"{ticket['description'] or 'Sin descripción'}"
        )

        if ticket["assigned_to"]:
            print(
                f"  Asignado a: "
                f"{ticket['assigned_to']}"
            )

        if ticket["url"]:
            print(f"  URL: {ticket['url']}")

        print("-" * 120)

    print()


def imprimir_ticket_nuevo(
    ticket: dict[str, str],
) -> None:
    print()
    print("!" * 120)
    print("🚨 NUEVO TICKET ASIGNADO")
    print(f"Fecha: {ahora()}")
    print(f"Número: {ticket['number']}")
    print(f"Tipo: {ticket['type'] or 'Sin tipo'}")
    print(
        f"Prioridad: "
        f"{ticket['priority'] or 'Sin prioridad'}"
    )
    print(
        f"Estado: "
        f"{ticket['state'] or 'Sin estado'}"
    )
    print(
        f"Asignado a: "
        f"{ticket['assigned_to'] or 'Sin asignar'}"
    )
    print(
        f"Descripción: "
        f"{ticket['description'] or 'Sin descripción'}"
    )

    if ticket["url"]:
        print(f"URL: {ticket['url']}")

    print("!" * 120)
    print()


# ==========================================================
# DISCORD
# ==========================================================

def notificar_discord(
    ticket: dict[str, str],
) -> None:
    """
    Un error de Discord no detiene el monitor.
    """

    try:
        enviar_ticket_nuevo(ticket)

        print(
            f"[{ahora()}] "
            f"Notificación enviada a Discord: "
            f"{ticket['number']}"
        )

    except DiscordNotificationError as exc:
        print(
            f"[{ahora()}] "
            f"No se pudo notificar a Discord: {exc}"
        )

    except Exception as exc:
        print(
            f"[{ahora()}] "
            f"Error inesperado notificando Discord: {exc}"
        )


# ==========================================================
# COMPARACIÓN
# ==========================================================

def procesar_tickets(
    tickets: list[dict[str, str]],
    mostrar_lista: bool = False,
) -> None:
    global tickets_conocidos
    global lista_inicial_mostrada

    actuales = {
        ticket["sys_id"]
        for ticket in tickets
    }

    print(
        f"[{ahora()}] "
        f"Consulta correcta: {len(tickets)} tickets."
    )

    if mostrar_lista and not lista_inicial_mostrada:
        imprimir_lista_inicial(tickets)
        lista_inicial_mostrada = True

    # Primera ejecución sin archivo de estado:
    # guarda la línea base y no notifica tickets viejos.
    if not STATE_FILE.exists() and not tickets_conocidos:
        tickets_conocidos = actuales
        guardar_tickets_conocidos(tickets_conocidos)

        print(
            "Línea base creada. "
            "Los tickets actuales no se notifican."
        )
        return

    nuevos_ids = actuales - tickets_conocidos

    if nuevos_ids:
        print(
            f"[{ahora()}] "
            f"Se detectaron {len(nuevos_ids)} tickets nuevos."
        )

    for ticket in tickets:
        if ticket["sys_id"] not in nuevos_ids:
            continue

        imprimir_ticket_nuevo(ticket)
        notificar_discord(ticket)

    # Solo conservamos los tickets que actualmente aparecen.
    # Si uno desaparece y después vuelve a ser asignado,
    # podrá volver a disparar una notificación.
    tickets_conocidos = actuales
    guardar_tickets_conocidos(tickets_conocidos)


# ==========================================================
# NAVEGACIÓN Y LOGIN
# ==========================================================

def navegar_seguro(
    page: Page,
    url: str,
    esperar: str = "commit",
) -> None:
    """
    ServiceNow puede provocar ERR_ABORTED mientras su router
    continúa navegando. Ese error se ignora únicamente en ese caso.
    """

    try:
        page.goto(
            url,
            wait_until=esperar,
            timeout=120_000,
        )

    except PlaywrightError as exc:
        if "ERR_ABORTED" not in str(exc):
            raise

        print(
            "ServiceNow abortó la navegación externa, "
            "pero se continuará esperando al workspace."
        )

    page.wait_for_timeout(3_000)


def iniciar_sesion(page: Page) -> None:
    print("Iniciando sesión...")

    navegar_seguro(
        page,
        LOGIN_URL,
        esperar="domcontentloaded",
    )

    try:
        page.locator("#user_name").wait_for(
            state="visible",
            timeout=30_000,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "No apareció el formulario de inicio de sesión."
        ) from exc

    page.locator("#user_name").fill(USERNAME)
    page.locator("#user_password").fill(PASSWORD)
    page.locator("#sysverb_login").click()

    try:
        page.wait_for_function(
            """
            () => {
                const url = location.href.toLowerCase();

                return !url.includes("login.do")
                    && !url.includes("session_timeout.do")
                    && !url.includes("logout.do");
            }
            """,
            timeout=60_000,
        )

    except PlaywrightTimeoutError as exc:
        mensaje = ""

        try:
            mensaje = (
                page.locator("#output_messages")
                .inner_text()
                .strip()
            )
        except Exception:
            pass

        if mensaje:
            raise RuntimeError(
                f"ServiceNow rechazó el login: {mensaje}"
            ) from exc

        raise RuntimeError(
            "No fue posible iniciar sesión. "
            "Revisa usuario, contraseña, MFA o SSO."
        ) from exc

    print("Sesión iniciada correctamente.")


# ==========================================================
# CAPTURA DEL DATA BROKER
# ==========================================================

def seleccionar_headers(
    headers: dict[str, str],
) -> dict[str, str]:
    permitidos = {
        "accept",
        "content-type",
        "x-usertoken",
        "x-user-token",
        "now-ui-interaction",
        "now-ux-interaction",
        "x-request-cancelable",
        "x-transaction-source",
    }

    seleccionados: dict[str, str] = {}

    for nombre, valor in headers.items():
        nombre = nombre.lower().strip()

        if nombre in permitidos:
            seleccionados[nombre] = valor

    seleccionados["accept"] = "application/json"
    seleccionados["content-type"] = "application/json"

    return seleccionados


def payload_es_lista_tareas(payload: Any) -> bool:
    elementos = (
        payload
        if isinstance(payload, list)
        else [payload]
    )

    for elemento in elementos:
        if not isinstance(elemento, dict):
            continue

        if (
            elemento.get("pipelineId")
            != "sn_record_list_composite_broker"
        ):
            continue

        input_values = elemento.get("inputValues", {})

        if not isinstance(input_values, dict):
            continue

        tabla = input_values.get("table", {})
        query = input_values.get("query", {})

        tabla_value = (
            tabla.get("value")
            if isinstance(tabla, dict)
            else None
        )

        query_value = (
            query.get("value")
            if isinstance(query, dict)
            else None
        )

        if (
            tabla_value == "task"
            and isinstance(query_value, str)
            and "assigned_to" in query_value
        ):
            return True

    return False


def capturar_request(request: Request) -> None:
    global payload_exec
    global headers_exec

    if EXEC_PATH not in request.url:
        return

    if request.method.upper() != "POST":
        return

    try:
        posible_payload = request.post_data_json
    except Exception:
        return

    if posible_payload is None:
        return

    if not payload_es_lista_tareas(posible_payload):
        return

    payload_exec = copy.deepcopy(posible_payload)

    try:
        headers = request.all_headers()
    except Exception:
        headers = request.headers

    headers_exec = seleccionar_headers(headers)

    print("Petición de My To-Do capturada.")


def capturar_response(response: Response) -> None:
    global respuesta_inicial

    if EXEC_PATH not in response.url:
        return

    if response.status != 200:
        return

    try:
        data = response.json()
    except Exception:
        return

    tickets = extraer_tickets(data)

    if tickets is None:
        return

    respuesta_inicial = data


def abrir_todo_y_capturar(page: Page) -> None:
    global payload_exec
    global headers_exec
    global respuesta_inicial

    payload_exec = None
    headers_exec = {}
    respuesta_inicial = None

    print("Abriendo My To-Do...")

    navegar_seguro(
        page,
        TODO_URL,
        esperar="commit",
    )

    if es_pagina_login(page.url):
        iniciar_sesion(page)

        navegar_seguro(
            page,
            TODO_URL,
            esperar="commit",
        )

    print(f"URL actual: {page.url}")
    print("Esperando respuesta inicial de My To-Do...")

    limite = time.time() + 90

    while time.time() < limite:
        if (
            payload_exec is not None
            and respuesta_inicial is not None
        ):
            break

        if es_pagina_login(page.url):
            raise PermissionError(
                "ServiceNow redirigió al login."
            )

        page.wait_for_timeout(500)

    if payload_exec is None:
        raise RuntimeError(
            "No se pudo capturar la petición de My To-Do."
        )

    if respuesta_inicial is None:
        raise RuntimeError(
            "La petición fue capturada, "
            "pero no llegó la respuesta con tickets."
        )

    print("Petición y respuesta inicial capturadas.")


# ==========================================================
# CONSULTA PERIÓDICA
# ==========================================================

def preparar_payload(payload_original: Any) -> Any:
    payload = copy.deepcopy(payload_original)
    request_id = uuid.uuid4().hex

    elementos = (
        payload
        if isinstance(payload, list)
        else [payload]
    )

    for elemento in elementos:
        if not isinstance(elemento, dict):
            continue

        input_values = elemento.get("inputValues", {})

        if not isinstance(input_values, dict):
            continue

        metadata = input_values.get(
            "requestMetadata",
            {},
        )

        if not isinstance(metadata, dict):
            continue

        metadata_value = metadata.get("value", {})

        if not isinstance(metadata_value, dict):
            continue

        metadata_value["requestId"] = request_id
        metadata_value["refreshRequested"] = True
        metadata_value["fromButton"] = True
        metadata_value["appendRows"] = False
        metadata_value["currentPage"] = 0

    return payload


def consultar_service_now(
    page: Page,
) -> dict[str, Any]:
    if payload_exec is None:
        raise RuntimeError(
            "No existe un payload capturado."
        )

    payload = preparar_payload(payload_exec)

    resultado = page.evaluate(
        """
        async ({url, headers, payload}) => {
            try {
                const response = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers,
                    body: JSON.stringify(payload),
                    cache: "no-store"
                });

                const text = await response.text();

                return {
                    status: response.status,
                    finalUrl: response.url,
                    contentType:
                        response.headers.get("content-type") || "",
                    text
                };

            } catch (error) {
                return {
                    status: 0,
                    finalUrl: "",
                    contentType: "",
                    text: "",
                    error: String(error)
                };
            }
        }
        """,
        {
            "url": EXEC_URL,
            "headers": headers_exec,
            "payload": payload,
        },
    )

    if not isinstance(resultado, dict):
        raise RuntimeError(
            "El navegador devolvió una respuesta inválida."
        )

    if resultado.get("error"):
        raise RuntimeError(
            f"Error de fetch: {resultado['error']}"
        )

    status = int(resultado.get("status", 0))

    final_url = str(
        resultado.get("finalUrl", "")
    ).lower()

    content_type = str(
        resultado.get("contentType", "")
    ).lower()

    texto = str(resultado.get("text", ""))

    if (
        status in (401, 403)
        or "login.do" in final_url
        or "session_timeout.do" in final_url
        or "text/html" in content_type
    ):
        raise PermissionError(
            "La sesión de ServiceNow expiró."
        )

    if status != 200:
        raise RuntimeError(
            f"Data Broker devolvió HTTP {status}: "
            f"{texto[:500]}"
        )

    try:
        data = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "La respuesta de ServiceNow no fue JSON."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            "El JSON no tiene el formato esperado."
        )

    return data


def recuperar_sesion(page: Page) -> None:
    print()
    print("Recuperando sesión de ServiceNow...")

    iniciar_sesion(page)
    abrir_todo_y_capturar(page)

    print("Sesión recuperada.")
    print()


# ==========================================================
# MAIN
# ==========================================================

def main() -> None:
    global tickets_conocidos

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

    if REFRESH_SECONDS < 10:
        raise RuntimeError("REFRESH_SECONDS debe ser al menos 10.")

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tickets_conocidos = cargar_tickets_conocidos()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
        )

        context = browser.new_context(
            locale="es-MX",
            timezone_id="America/Mexico_City",
        )

        page = context.new_page()

        page.on("request", capturar_request)
        page.on("response", capturar_response)

        try:
            iniciar_sesion(page)
            abrir_todo_y_capturar(page)

            if respuesta_inicial is None:
                raise RuntimeError(
                    "No existe una respuesta inicial."
                )

            tickets_iniciales = extraer_tickets(
                respuesta_inicial
            )

            if tickets_iniciales is None:
                raise RuntimeError(
                    "La respuesta inicial no contiene "
                    "la lista de tickets."
                )

            procesar_tickets(
                tickets_iniciales,
                mostrar_lista=True,
            )

            print()
            print("Monitor activo sin interfaz gráfica.")
            print(
                f"Consulta cada "
                f"{REFRESH_SECONDS} segundos."
            )
            print(
                "Las nuevas asignaciones se enviarán "
                "automáticamente a Discord."
            )
            print("Presiona Ctrl+C para detenerlo.")
            print()

            while True:
                time.sleep(REFRESH_SECONDS)

                try:
                    data = consultar_service_now(page)
                    tickets = extraer_tickets(data)

                    if tickets is None:
                        print(
                            f"[{ahora()}] "
                            "La respuesta no correspondía "
                            "a My To-Do."
                        )
                        continue

                    procesar_tickets(tickets)

                except PermissionError:
                    try:
                        recuperar_sesion(page)

                        if respuesta_inicial is not None:
                            tickets = extraer_tickets(
                                respuesta_inicial
                            )

                            if tickets is not None:
                                procesar_tickets(tickets)

                    except Exception as exc:
                        print(
                            f"[{ahora()}] "
                            f"No se pudo recuperar "
                            f"la sesión: {exc}"
                        )

                except Exception as exc:
                    print(
                        f"[{ahora()}] "
                        f"Error consultando ServiceNow: "
                        f"{exc}"
                    )

        except KeyboardInterrupt:
            print()
            print("Monitor detenido.")

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
