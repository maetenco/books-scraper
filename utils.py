"""
Utilidades compartidas entre dashboard.py y telegram.py.

Antes `formatear_precio()` estaba duplicada literalmente en ambos
módulos (mismo cuerpo, mismo formato "$12.990"). Vive aquí para que
un cambio futuro de formato (p. ej. soportar otra moneda) se haga en
un solo lugar.
"""

from datetime import date
from pathlib import Path

# ── Marca de última ejecución exitosa ──
# Se usa en run.py para no scrapear dos veces el mismo día cuando
# la tarea programada tiene más de un disparador (p. ej. horario
# diario fijo + "al iniciar sesión", agregado porque el equipo puede
# estar apagado/suspendido a la hora fija).
_MARCA_ULTIMA_EJECUCION = Path(__file__).parent / ".last_run"


# type hint: valor: int | None, retorno -> str
# Mejora: mypy detecta si le pasas algo que no sea int;
#         el retorno str siempre es renderizable (nunca None).
def formatear_precio(valor: int | None) -> str:
    if valor is None:
        return "—"
    return f"${valor:,.0f}".replace(",", ".")


# type hint: retorno -> bool
# Mejora: aislar la lectura del archivo de marca en una función propia
#         permite testearla sin tocar el disco real (mock de Path).
def ya_corrio_hoy() -> bool:
    """True si ya hubo una ejecución exitosa hoy (fecha local)."""
    if not _MARCA_ULTIMA_EJECUCION.exists():
        return False
    contenido = _MARCA_ULTIMA_EJECUCION.read_text(encoding="utf-8").strip()
    return contenido == date.today().isoformat()


# type hint: retorno -> None
def marcar_ejecucion_hoy() -> None:
    """Registra la fecha de hoy como última ejecución exitosa."""
    _MARCA_ULTIMA_EJECUCION.write_text(date.today().isoformat(), encoding="utf-8")
