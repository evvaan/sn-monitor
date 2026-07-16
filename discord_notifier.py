from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger("sn_monitor.discord")


# =============================================================================
# CONFIGURACIÓN
# =============================================================================


def env_bool(nombre: str, default: bool) -> bool:
    valor = os.getenv(nombre)
    if valor is None:
        return default
    return valor.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def env_int(nombre: str, default: int, minimo: int = 1) -> int:
    valor = os.getenv(nombre)
    if valor is None or not valor.strip():
        resultado = default
    else:
        try:
            resultado = int(valor)
        except ValueError as exc:
            raise RuntimeError(f"{nombre} debe ser un entero.") from exc

    if resultado < minimo:
        raise RuntimeError(f"{nombre} debe ser al menos {minimo}.")
    return resultado


DISCORD_ENABLED = env_bool("DISCORD_ENABLED", True)
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USERNAME = os.getenv("DISCORD_USERNAME", "ServiceNow Monitor").strip()
REQUEST_TIMEOUT_SECONDS = env_int("REQUEST_TIMEOUT_SECONDS", 20, 5)
DISCORD_MAX_ATTEMPTS = env_int("DISCORD_MAX_ATTEMPTS", 4, 1)
DISCORD_MAX_BACKOFF_SECONDS = env_int(
    "DISCORD_MAX_BACKOFF_SECONDS", 60, 1
)

COLOR_RED = 15_548_972
COLOR_ORANGE = 15_105_570
COLOR_BLUE = 3_447_003
COLOR_GREEN = 5_766_719
COLOR_GRAY = 9_934_993

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "sn-monitor/2.0"})


class DiscordNotificationError(RuntimeError):
    """Error al enviar una notificación hacia Discord."""


def _limitar_texto(
    valor: Any,
    limite: int,
    valor_vacio: str = "No disponible",
) -> str:
    texto = str(valor or "").strip()
    if not texto:
        return valor_vacio
    if len(texto) <= limite:
        return texto
    return texto[: limite - 3] + "..."


def _color_por_prioridad(prioridad: str) -> int:
    prioridad_normalizada = prioridad.lower().strip()
    if prioridad_normalizada.startswith("1"):
        return COLOR_RED
    if prioridad_normalizada.startswith("2"):
        return COLOR_ORANGE
    if prioridad_normalizada.startswith("3"):
        return COLOR_BLUE
    if prioridad_normalizada.startswith("4"):
        return COLOR_GREEN
    return COLOR_GRAY


def _validar_webhook() -> None:
    if not DISCORD_ENABLED:
        return

    if not DISCORD_WEBHOOK_URL:
        raise DiscordNotificationError(
            "La URL del webhook de Discord está vacía."
        )

    if "PEGA_AQUI" in DISCORD_WEBHOOK_URL:
        raise DiscordNotificationError(
            "Debes reemplazar el valor de ejemplo por el webhook real."
        )

    if not DISCORD_WEBHOOK_URL.startswith(
        (
            "https://discord.com/api/webhooks/",
            "https://discordapp.com/api/webhooks/",
        )
    ):
        raise DiscordNotificationError(
            "La URL configurada no parece ser un webhook válido de Discord."
        )


def _backoff(intento: int) -> float:
    return float(
        min(
            DISCORD_MAX_BACKOFF_SECONDS,
            max(1, 2 ** (intento - 1)),
        )
    )


def _retry_after(response: requests.Response, intento: int) -> float:
    cabecera = response.headers.get("Retry-After", "").strip()
    if cabecera:
        try:
            return min(
                DISCORD_MAX_BACKOFF_SECONDS,
                max(0.5, float(cabecera)),
            )
        except ValueError:
            pass

    try:
        data = response.json()
        if isinstance(data, dict) and "retry_after" in data:
            return min(
                DISCORD_MAX_BACKOFF_SECONDS,
                max(0.5, float(data["retry_after"])),
            )
    except (ValueError, TypeError, json.JSONDecodeError):
        pass

    return _backoff(intento)


def enviar_mensaje_simple(
    mensaje: str,
    mencionar_everyone: bool = False,
) -> None:
    _validar_webhook()

    if not DISCORD_ENABLED:
        logger.info("Discord está deshabilitado; mensaje omitido.")
        return

    contenido = _limitar_texto(
        mensaje,
        limite=1_900,
        valor_vacio="Notificación sin contenido.",
    )

    if mencionar_everyone:
        contenido = f"@everyone\n{contenido}"

    payload = {
        "username": _limitar_texto(
            DISCORD_USERNAME, 80, "ServiceNow Monitor"
        ),
        "content": contenido,
        "allowed_mentions": {
            "parse": ["everyone"] if mencionar_everyone else [],
        },
    }
    _enviar_payload(payload)


def enviar_ticket_nuevo(ticket: dict[str, str]) -> None:
    _validar_webhook()

    if not DISCORD_ENABLED:
        logger.info(
            "Discord está deshabilitado; ticket %s marcado sin envío.",
            ticket.get("number", "sin número"),
        )
        return

    numero = _limitar_texto(
        ticket.get("number"),
        limite=256,
        valor_vacio="Ticket sin número",
    )
    descripcion = _limitar_texto(
        ticket.get("description"),
        limite=2_000,
        valor_vacio="Sin descripción",
    )
    prioridad = _limitar_texto(
        ticket.get("priority"),
        limite=1_024,
        valor_vacio="Sin prioridad",
    )
    estado = _limitar_texto(
        ticket.get("state"),
        limite=1_024,
        valor_vacio="Sin estado",
    )
    tipo = _limitar_texto(
        ticket.get("type"),
        limite=1_024,
        valor_vacio="Sin tipo",
    )
    asignado = _limitar_texto(
        ticket.get("assigned_to"),
        limite=1_024,
        valor_vacio="Sin asignar",
    )
    url = str(ticket.get("url") or "").strip()

    embed: dict[str, Any] = {
        "title": f"Nuevo ticket: {numero}",
        "description": descripcion,
        "color": _color_por_prioridad(prioridad),
        "fields": [
            {
                "name": "Prioridad",
                "value": prioridad,
                "inline": True,
            },
            {
                "name": "Estado",
                "value": estado,
                "inline": True,
            },
            {
                "name": "Tipo",
                "value": tipo,
                "inline": True,
            },
            {
                "name": "Asignado a",
                "value": asignado,
                "inline": False,
            },
        ],
        "footer": {
            "text": "Monitor automático de ServiceNow",
        },
    }

    if url.startswith(("http://", "https://")):
        embed["url"] = url

    payload = {
        "username": _limitar_texto(
            DISCORD_USERNAME, 80, "ServiceNow Monitor"
        ),
        "content": "**Se detectó una nueva asignación**",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    _enviar_payload(payload)


def _enviar_payload(payload: dict[str, Any]) -> None:
    """
    Envía el webhook con confirmación (`wait=true`).

    Reintenta timeouts, errores de conexión, HTTP 429 y errores 5xx. Los errores
    permanentes 4xx se reportan inmediatamente sin imprimir la URL secreta.
    """
    ultimo_error = ""

    for intento in range(1, DISCORD_MAX_ATTEMPTS + 1):
        response: requests.Response | None = None

        try:
            response = _SESSION.post(
                DISCORD_WEBHOOK_URL,
                params={"wait": "true"},
                json=payload,
                timeout=(REQUEST_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS),
            )

            if response.status_code in {200, 204}:
                return

            if response.status_code == 429:
                espera = _retry_after(response, intento)
                ultimo_error = "Discord aplicó rate limit HTTP 429."

                if intento < DISCORD_MAX_ATTEMPTS:
                    logger.warning(
                        "%s Reintento %s/%s en %.1f segundos.",
                        ultimo_error,
                        intento + 1,
                        DISCORD_MAX_ATTEMPTS,
                        espera,
                    )
                    time.sleep(espera)
                    continue

                raise DiscordNotificationError(
                    f"{ultimo_error} Se agotaron los reintentos."
                )

            if 500 <= response.status_code <= 599:
                ultimo_error = (
                    f"Discord devolvió HTTP transitorio {response.status_code}."
                )

                if intento < DISCORD_MAX_ATTEMPTS:
                    espera = _backoff(intento)
                    logger.warning(
                        "%s Reintento %s/%s en %.1f segundos.",
                        ultimo_error,
                        intento + 1,
                        DISCORD_MAX_ATTEMPTS,
                        espera,
                    )
                    time.sleep(espera)
                    continue

                raise DiscordNotificationError(
                    f"{ultimo_error} Se agotaron los reintentos."
                )

            detalle = _limitar_texto(
                response.text,
                limite=500,
                valor_vacio="Sin detalle",
            )
            raise DiscordNotificationError(
                f"Discord devolvió HTTP {response.status_code}: {detalle}"
            )

        except requests.Timeout as exc:
            ultimo_error = "Discord no respondió antes del timeout."

            if intento < DISCORD_MAX_ATTEMPTS:
                espera = _backoff(intento)
                logger.warning(
                    "%s Reintento %s/%s en %.1f segundos.",
                    ultimo_error,
                    intento + 1,
                    DISCORD_MAX_ATTEMPTS,
                    espera,
                )
                time.sleep(espera)
                continue

            raise DiscordNotificationError(ultimo_error) from exc

        except requests.RequestException as exc:
            # No incluir str(exc): requests puede incorporar la URL completa del
            # webhook, que contiene credenciales secretas.
            ultimo_error = (
                "No fue posible conectar con Discord "
                f"({type(exc).__name__})."
            )

            if intento < DISCORD_MAX_ATTEMPTS:
                espera = _backoff(intento)
                logger.warning(
                    "%s Reintento %s/%s en %.1f segundos.",
                    ultimo_error,
                    intento + 1,
                    DISCORD_MAX_ATTEMPTS,
                    espera,
                )
                time.sleep(espera)
                continue

            raise DiscordNotificationError(ultimo_error) from exc

        finally:
            if response is not None:
                response.close()

    raise DiscordNotificationError(
        ultimo_error or "No fue posible enviar la notificación a Discord."
    )
