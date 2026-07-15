from __future__ import annotations

from typing import Any

import os
import requests


# ==========================================================
# CONFIGURACIÓN
# ==========================================================

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_USERNAME = os.getenv("DISCORD_USERNAME", "ServiceNow Monitor")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))


# Colores decimales usados en los embeds.
COLOR_RED = 15_548_972
COLOR_ORANGE = 15_105_570
COLOR_BLUE = 3_447_003
COLOR_GREEN = 5_766_719
COLOR_GRAY = 9_934_993


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
    if not DISCORD_WEBHOOK_URL:
        raise DiscordNotificationError(
            "La URL del webhook de Discord está vacía."
        )

    if "PEGA_AQUI" in DISCORD_WEBHOOK_URL:
        raise DiscordNotificationError(
            "Debes reemplazar PEGA_AQUI_TU_WEBHOOK_COMPLETO "
            "por la URL real del webhook."
        )

    if not DISCORD_WEBHOOK_URL.startswith(
        (
            "https://discord.com/api/webhooks/",
            "https://discordapp.com/api/webhooks/",
        )
    ):
        raise DiscordNotificationError(
            "La URL configurada no parece ser un webhook válido "
            "de Discord."
        )


def enviar_mensaje_simple(
    mensaje: str,
    mencionar_everyone: bool = False,
) -> None:
    """
    Envía un mensaje de texto sencillo.

    mencionar_everyone=True intenta enviar @everyone.
    Para recibir avisos en el móvil no debería ser necesario si
    configuras el canal para notificar todos los mensajes.
    """

    _validar_webhook()

    contenido = _limitar_texto(
        mensaje,
        limite=1_900,
        valor_vacio="Notificación sin contenido.",
    )

    if mencionar_everyone:
        contenido = f"@everyone\n{contenido}"

    payload = {
        "username": DISCORD_USERNAME,
        "content": contenido,
        "allowed_mentions": {
            "parse": ["everyone"] if mencionar_everyone else [],
        },
    }

    _enviar_payload(payload)


def enviar_ticket_nuevo(ticket: dict[str, str]) -> None:
    """
    Envía a Discord un ticket nuevo usando un embed.
    """

    _validar_webhook()

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
        "title": f"🚨 Nuevo ticket: {numero}",
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
        "username": DISCORD_USERNAME,
        "content": "🔔 **Se detectó una nueva asignación**",
        "embeds": [embed],
        "allowed_mentions": {
            "parse": [],
        },
    }

    _enviar_payload(payload)


def _enviar_payload(payload: dict[str, Any]) -> None:
    """
    Ejecuta el webhook y solicita confirmación de Discord usando wait=true.
    """

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            params={"wait": "true"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise DiscordNotificationError(
            "Discord no respondió antes del timeout."
        ) from exc
    except requests.RequestException as exc:
        raise DiscordNotificationError(
            f"No fue posible conectar con Discord: {exc}"
        ) from exc

    if response.status_code not in (200, 204):
        detalle = response.text[:500]

        raise DiscordNotificationError(
            f"Discord devolvió HTTP {response.status_code}: "
            f"{detalle}"
        )
