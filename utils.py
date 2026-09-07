"""
Utilidades compartidas entre dashboard.py y telegram.py.

Antes `formatear_precio()` estaba duplicada literalmente en ambos
módulos (mismo cuerpo, mismo formato "$12.990"). Vive aquí para que
un cambio futuro de formato (p. ej. soportar otra moneda) se haga en
un solo lugar.
"""


# type hint: valor: int | None, retorno -> str
# Mejora: mypy detecta si le pasas algo que no sea int;
#         el retorno str siempre es renderizable (nunca None).
def formatear_precio(valor: int | None) -> str:
    if valor is None:
        return "—"
    return f"${valor:,.0f}".replace(",", ".")
