"""
actualizar_donantes.py — Genera la lista de mecenas que consume la app.

Autónomo: sin dependencias del proyecto, solo `requests`. La copia de
producción está en Emy69/Proyect-C, movida por una acción programada.

Al JSON solo van nombres. Los importes ordenan la lista y se descartan; el
orden es lo único que conserva el ranking. Con `ordenar=False` se publica por
orden de aparición.

Modos:
    python actualizar_donantes.py sembrar volcado.sql
    python actualizar_donantes.py actualizar

`actualizar` fusiona con lo ya publicado en vez de sustituirlo.

Entorno requerido por `actualizar`:
    PATREON_CLIENT_ID, PATREON_CLIENT_SECRET, PATREON_REFRESH_TOKEN
    PATREON_CAMPAIGN_ID (opcional; si falta se consulta a la API)
"""

import json
import os
import re
import sys
from datetime import date

RUTA_JSON = "donadores.json"
API = "https://www.patreon.com/api/oauth2"


# ── volcado SQL ──────────────────────────────────────────────────────────────
def _valores_sql(tupla: str) -> list:
    """Parte una tupla de VALUES respetando comillas y escapes.

    Los nombres pueden contener comas y apóstrofos escapados.
    """
    salida, actual, dentro, escapando = [], [], False, False
    for ch in tupla:
        if escapando:
            actual.append(ch)
            escapando = False
        elif ch == "\\" and dentro:
            escapando = True
        elif ch == "'":
            dentro = not dentro
            if not dentro:
                salida.append("".join(actual))
                actual = []
        elif dentro:
            actual.append(ch)
        elif ch == ",":
            if actual:
                salida.append("".join(actual).strip())
                actual = []
            elif not salida or not tupla:
                salida.append("")
        else:
            actual.append(ch)
    if actual:
        salida.append("".join(actual).strip())
    return salida


def leer_volcado_sql(texto: str) -> list:
    """[(nombre, importe)] de un volcado de phpMyAdmin de `donors`."""
    filas = []
    for bloque in re.findall(r"INSERT INTO\s+`?donors`?[^;]*?VALUES\s*(.*?);",
                             texto, re.S | re.I):
        for tupla in re.findall(r"\(((?:[^()']|'(?:\\.|[^'\\])*')*)\)", bloque, re.S):
            v = _valores_sql(tupla)
            if len(v) < 5:
                continue
            nombre = (v[1] or "").strip()
            try:
                importe = float(v[4])
            except (TypeError, ValueError):
                importe = 0.0
            if nombre:
                filas.append((nombre, importe))
    return filas


# ── lista ────────────────────────────────────────────────────────────────────
def _sin_repetidos(nombres) -> list:
    """Conserva la primera aparición, ignorando mayúsculas y espacios."""
    vistos, salida = set(), []
    for n in nombres:
        n = " ".join(str(n or "").split())
        clave = n.casefold()
        if n and clave not in vistos:
            vistos.add(clave)
            salida.append(n)
    return salida


def nombres_ordenados(filas, ordenar: bool = True) -> list:
    """De [(nombre, importe)] a la lista publicable. El importe no viaja."""
    if ordenar:
        filas = sorted(filas, key=lambda f: -float(f[1] or 0))
    return _sin_repetidos(n for n, _ in filas)


def fusionar(previos, actuales) -> list:
    """Actuales primero; detrás los previos que Patreon ya no devuelve."""
    actuales = _sin_repetidos(actuales)
    ya = {n.casefold() for n in actuales}
    return actuales + [n for n in _sin_repetidos(previos) if n.casefold() not in ya]


def leer_json(ruta: str = RUTA_JSON) -> list:
    """Nombres ya publicados. Acepta el formato antiguo (objetos con `name`)."""
    try:
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(datos, dict):
        datos = datos.get("donantes") or datos.get("donors") or []
    return [d.get("name", "") if isinstance(d, dict) else str(d) for d in datos]


def escribir_json(nombres, ruta: str = RUTA_JSON) -> dict:
    datos = {"actualizado": date.today().isoformat(),
             "total": len(nombres),
             "donantes": list(nombres)}
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return datos


# ── Patreon ──────────────────────────────────────────────────────────────────
def refrescar_token(client_id, client_secret, refresh_token, sesion=None) -> dict:
    """Canjea el refresh token. Se ejecuta siempre: el del creador caduca."""
    import requests
    s = sesion or requests
    r = s.post(f"{API}/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": client_secret}, timeout=30)
    r.raise_for_status()
    return r.json()


def id_de_campana(token, sesion=None) -> str:
    import requests
    s = sesion or requests
    r = s.get(f"{API}/v2/campaigns", headers={"Authorization": f"Bearer {token}"},
              timeout=30)
    r.raise_for_status()
    datos = r.json().get("data") or []
    if not datos:
        raise RuntimeError("la cuenta no tiene ninguna campaña")
    return datos[0]["id"]


def miembros(token, campana, sesion=None) -> list:
    """[(nombre, centavos aportados en total)] de la campaña, paginado.

    Usa `campaign_lifetime_support_cents`, no el pago vigente, e incluye a los
    que ya no son mecenas.
    """
    import requests
    s = sesion or requests
    url = f"{API}/v2/campaigns/{campana}/members"
    params = {"fields[member]": "full_name,campaign_lifetime_support_cents,patron_status",
              "page[count]": "1000"}
    salida = []
    while url:
        r = s.get(url, params=params, timeout=60,
                  headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        cuerpo = r.json()
        for m in (cuerpo.get("data") or []):
            a = m.get("attributes") or {}
            nombre = (a.get("full_name") or "").strip()
            if nombre:
                salida.append((nombre, int(a.get("campaign_lifetime_support_cents") or 0)))
        url = ((cuerpo.get("links") or {}).get("next") or "")
        params = None          # el enlace de la siguiente página ya los trae
    return salida


# ── órdenes ──────────────────────────────────────────────────────────────────
def sembrar(ruta_sql: str, ruta_json: str = RUTA_JSON) -> dict:
    with open(ruta_sql, encoding="utf-8", errors="replace") as f:
        filas = leer_volcado_sql(f.read())
    if not filas:
        raise SystemExit(f"no se encontró ninguna fila de donors en {ruta_sql}")
    datos = escribir_json(nombres_ordenados(filas), ruta_json)
    print(f"{datos['total']} nombres escritos en {ruta_json} "
          f"(de {len(filas)} filas del volcado)")
    return datos


def actualizar(ruta_json: str = RUTA_JSON) -> dict:
    faltan = [v for v in ("PATREON_CLIENT_ID", "PATREON_CLIENT_SECRET",
                          "PATREON_REFRESH_TOKEN") if not os.environ.get(v)]
    if faltan:
        raise SystemExit("faltan variables de entorno: " + ", ".join(faltan))
    tok = refrescar_token(os.environ["PATREON_CLIENT_ID"],
                          os.environ["PATREON_CLIENT_SECRET"],
                          os.environ["PATREON_REFRESH_TOKEN"])
    acceso = tok["access_token"]
    nuevo_refresh = tok.get("refresh_token") or ""
    campana = os.environ.get("PATREON_CAMPAIGN_ID") or id_de_campana(acceso)
    filas = miembros(acceso, campana)
    print(f"Patreon devolvió {len(filas)} miembros de la campaña {campana}")
    datos = escribir_json(
        fusionar(leer_json(ruta_json), nombres_ordenados(filas)), ruta_json)
    print(f"{datos['total']} nombres en {ruta_json}")
    # Expuesto para que el workflow lo reescriba como secreto.
    salida_gh = os.environ.get("GITHUB_OUTPUT")
    if salida_gh and nuevo_refresh:
        with open(salida_gh, "a", encoding="utf-8") as f:
            f.write(f"refresh_token={nuevo_refresh}\n")
    return datos


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    orden = argv[0] if argv else ""
    if orden == "sembrar" and len(argv) >= 2:
        sembrar(argv[1], argv[2] if len(argv) > 2 else RUTA_JSON)
    elif orden == "actualizar":
        actualizar(argv[1] if len(argv) > 1 else RUTA_JSON)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
