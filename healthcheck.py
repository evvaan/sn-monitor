from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HEALTH_FILE = Path(os.getenv("HEALTH_FILE", "/run/sn-monitor/health.json"))
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "30"))
HEALTH_MAX_AGE_SECONDS = int(
    os.getenv("HEALTH_MAX_AGE_SECONDS", str(max(300, REFRESH_SECONDS * 4)))
)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main() -> int:
    if not HEALTH_FILE.exists():
        print(f"No existe {HEALTH_FILE}", file=sys.stderr)
        return 1

    try:
        data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Health file inválido: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("Health file no es un objeto JSON.", file=sys.stderr)
        return 1

    status = str(data.get("status", ""))
    if status in {"fatal", "stopped"}:
        print(f"Estado no saludable: {status}", file=sys.stderr)
        return 1

    last_success = data.get("last_success_utc")
    if not isinstance(last_success, str) or not last_success:
        print("Todavía no existe una consulta correcta.", file=sys.stderr)
        return 1

    try:
        edad = (datetime.now(timezone.utc) - parse_utc(last_success)).total_seconds()
    except (TypeError, ValueError) as exc:
        print(f"last_success_utc inválido: {exc}", file=sys.stderr)
        return 1

    if edad > HEALTH_MAX_AGE_SECONDS:
        print(
            f"La última consulta correcta ocurrió hace {edad:.0f}s; "
            f"máximo permitido {HEALTH_MAX_AGE_SECONDS}s.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: status={status}, last_success_age={edad:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
