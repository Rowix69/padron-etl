"""Pipeline ETL para completar un padrón a partir de una lista de DNIs.

Extract   : lee el Excel y localiza dinámicamente la columna de DNI.
Transform : normaliza DNIs (8 dígitos), consulta la API de Decolecta con
            rotación de tokens y ordena el padrón alfabéticamente.
Load      : escribe el padrón completado en un nuevo Excel.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

API_URL = "https://api.decolecta.com/v1/reniec/dni"
TIMEOUT_SEGUNDOS = 10
MAX_REINTENTOS = 3
PAUSA_ENTRE_CONSULTAS = 0.3
MIN_DIGITOS = 6  # un DNI que perdió ceros a la izquierda en Excel conserva >= 6 dígitos

# Cabeceras ya normalizadas (minúsculas, sin tildes, sin símbolos ni espacios)
ALIAS_DNI = {
    "dni", "documento", "doc", "documentoidentidad", "documentodeidentidad",
    "numerodni", "nrodni", "ndni", "numerodocumento", "nrodocumento",
    "ndocumento", "numerodedocumento", "nrodedocumento",
}

log = logging.getLogger("padron_etl")


class TokensAgotadosError(Exception):
    """Todos los tokens fueron rechazados o agotaron su cuota."""


class TokenPool:
    """Rotación secuencial de tokens. Nunca expone su valor en los logs."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._indice = 0

    @property
    def agotado(self) -> bool:
        return self._indice >= len(self._tokens)

    @property
    def actual(self) -> str:
        if self.agotado:
            raise TokensAgotadosError("No quedan tokens disponibles.")
        return self._tokens[self._indice]

    def rotar(self) -> None:
        self._indice += 1
        if self.agotado:
            log.warning("Se agotaron todos los tokens.")
        else:
            log.info("Rotando al token %d de %d.", self._indice + 1, len(self._tokens))


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def quitar_tildes(texto) -> str:
    descompuesto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in descompuesto if not unicodedata.combining(c)).lower()


def normalizar_cabecera(nombre) -> str:
    """'  Documento de Identidad ' -> 'documentodeidentidad'."""
    return re.sub(r"[^a-z0-9]", "", quitar_tildes(nombre))


def normalizar_dni(valor) -> str | None:
    """Devuelve el DNI como string de 8 dígitos o None si no es válido."""
    if valor is None or pd.isna(valor):
        return None
    texto = str(valor).strip().replace(" ", "").replace("-", "")
    texto = re.sub(r"\.0+$", "", texto)  # '12345678.0' -> '12345678'
    if not re.fullmatch(r"[0-9]+", texto) or not (MIN_DIGITOS <= len(texto) <= 8):
        return None
    return texto.zfill(8)


def enmascarar(dni: str) -> str:
    """Evita escribir DNIs completos en los logs."""
    return f"{dni[:2]}****{dni[-2:]}"


def cargar_tokens() -> list[str]:
    crudo = os.getenv("DECOLECTA_TOKENS", "")
    tokens = [t.strip() for t in crudo.split(",") if t.strip()]
    if not tokens:
        raise SystemExit(
            "No se encontraron tokens. Define DECOLECTA_TOKENS en tu archivo .env "
            "(separados por comas). Revisa .env.example."
        )
    log.info("Tokens cargados: %d (capacidad aprox.: %d consultas).", len(tokens), len(tokens) * 100)
    return tokens


# ----------------------------------------------------------------------------
# EXTRACT
# ----------------------------------------------------------------------------
def detectar_columna_dni(df: pd.DataFrame):
    """Encuentra la columna de DNI sin depender de su posición ni de su nombre exacto.

    Estrategia en cascada:
      1. Cabecera igual a un alias conocido ('dni', 'Documento', 'N° DNI'...).
      2. Cabecera que contenga 'dni' o 'documento'.
      3. Por contenido: la columna donde >= 80% de los valores parecen un DNI.
    """
    normalizadas = {col: normalizar_cabecera(col) for col in df.columns}

    for col, norm in normalizadas.items():
        if norm in ALIAS_DNI:
            return col

    for col, norm in normalizadas.items():
        if "dni" in norm or "documento" in norm:
            return col

    mejor_col, mejor_ratio = None, 0.0
    for col in df.columns:
        serie = df[col].dropna()
        if serie.empty:
            continue
        ratio = serie.map(lambda v: normalizar_dni(v) is not None).mean()
        if ratio > mejor_ratio:
            mejor_col, mejor_ratio = col, ratio

    if mejor_col is not None and mejor_ratio >= 0.8:
        log.warning("Cabecera de DNI no reconocida; se infirió por contenido.")
        return mejor_col

    raise ValueError(
        f"No se pudo identificar la columna de DNI. Columnas encontradas: {list(df.columns)}"
    )


def extraer_dnis(ruta: Path, hoja: str | int = 0) -> list[str]:
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró el archivo de entrada: {ruta}")

    # dtype=str evita que pandas convierta los DNIs a float (y pierda ceros)
    df = pd.read_excel(ruta, sheet_name=hoja, dtype=str)
    columna = detectar_columna_dni(df)
    log.info("Columna de DNI detectada: '%s'", columna)

    normalizados = df[columna].map(normalizar_dni)
    validos = normalizados.dropna()
    unicos = list(dict.fromkeys(validos))  # deduplica conservando el orden

    log.info(
        "Filas: %d | DNIs únicos válidos: %d | duplicados: %d | vacíos/inválidos: %d",
        len(df), len(unicos), len(validos) - len(unicos), len(df) - len(validos),
    )
    return unicos


# ----------------------------------------------------------------------------
# TRANSFORM (consulta a la API + estructuración)
# ----------------------------------------------------------------------------
def consultar_dni(
    session: requests.Session, dni: str, pool: TokenPool
) -> tuple[str, dict | None]:
    """Consulta un DNI. Devuelve (estado, datos).

    Estados: OK | NO_ENCONTRADO | ERROR_RED | ERROR_RESPUESTA | ERROR_HTTP_<código>
    Lanza TokensAgotadosError si ningún token puede atender la consulta.
    """
    fallos_transitorios = 0
    while True:
        token = pool.actual  # lanza TokensAgotadosError si ya no quedan
        try:
            res = session.get(
                API_URL,
                params={"numero": dni},
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT_SEGUNDOS,
            )
        except requests.RequestException:
            res = None

        if res is not None:
            if res.status_code == 200:
                try:
                    return "OK", res.json()
                except ValueError:
                    return "ERROR_RESPUESTA", None
            if res.status_code in (401, 403, 429):  # token inválido o cuota agotada
                pool.rotar()
                time.sleep(1)
                continue
            if res.status_code in (404, 422):
                return "NO_ENCONTRADO", None
            if res.status_code < 500:
                return f"ERROR_HTTP_{res.status_code}", None

        # Error de red o 5xx: reintento con backoff exponencial (2s, 4s...)
        fallos_transitorios += 1
        if fallos_transitorios >= MAX_REINTENTOS:
            return ("ERROR_RED" if res is None else f"ERROR_HTTP_{res.status_code}"), None
        time.sleep(2 ** fallos_transitorios)


def construir_registro(dni: str, estado: str, persona: dict | None) -> dict:
    persona = persona or {}
    ape_pat = persona.get("first_last_name", "")
    ape_mat = persona.get("second_last_name", "")
    nombres = persona.get("first_name", "")
    nombre_completo = persona.get("full_name") or f"{ape_pat} {ape_mat} {nombres}".strip()
    return {
        "DNI": dni,
        "Apellido_Paterno": ape_pat,
        "Apellido_Materno": ape_mat,
        "Nombres": nombres,
        "Nombre_Completo": nombre_completo,
        "Estado": estado,
    }


def transformar(registros: list[dict]) -> pd.DataFrame:
    """Ordena alfabéticamente (sin distinguir tildes); los no resueltos van al final."""
    df = pd.DataFrame(registros)
    df["_falla"] = df["Estado"] != "OK"
    for col in ("Apellido_Paterno", "Apellido_Materno", "Nombres"):
        df[f"_k_{col}"] = df[col].fillna("").map(quitar_tildes)

    df = df.sort_values(
        ["_falla", "_k_Apellido_Paterno", "_k_Apellido_Materno", "_k_Nombres"]
    )
    columnas_auxiliares = [c for c in df.columns if c.startswith("_")]
    return df.drop(columns=columnas_auxiliares).reset_index(drop=True)


# ----------------------------------------------------------------------------
# LOAD
# ----------------------------------------------------------------------------
def cargar(df: pd.DataFrame, ruta: Path) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_excel(ruta, index=False)
    except PermissionError:
        raise SystemExit(f"No se pudo escribir '{ruta}'. Si está abierto en Excel, ciérralo.")


# ----------------------------------------------------------------------------
# Orquestación
# ----------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Completa un padrón vecinal a partir de DNIs.")
    p.add_argument("-i", "--input", type=Path, default=Path("data/lista.xlsx"),
                   help="Excel de entrada (default: data/lista.xlsx)")
    p.add_argument("-o", "--output", type=Path, default=Path("data/padron_completado.xlsx"),
                   help="Excel de salida (default: data/padron_completado.xlsx)")
    p.add_argument("--hoja", default="0", help="Nombre o índice de la hoja (default: 0)")
    args = p.parse_args()
    args.hoja = int(args.hoja) if str(args.hoja).isdigit() else args.hoja
    return args


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = parse_args()
    load_dotenv()

    pool = TokenPool(cargar_tokens())
    dnis = extraer_dnis(args.input, args.hoja)
    if not dnis:
        log.error("No hay DNIs válidos para procesar.")
        return 1

    registros: list[dict] = []
    try:
        with requests.Session() as session:
            for i, dni in enumerate(dnis, start=1):
                estado, persona = consultar_dni(session, dni, pool)
                registros.append(construir_registro(dni, estado, persona))
                if estado != "OK":
                    log.warning("DNI %s -> %s", enmascarar(dni), estado)
                if i % 25 == 0 or i == len(dnis):
                    log.info("Progreso: %d/%d", i, len(dnis))
                time.sleep(PAUSA_ENTRE_CONSULTAS)
    except TokensAgotadosError:
        log.error("Tokens agotados. Agrega más tokens al .env y vuelve a ejecutar.")
    except KeyboardInterrupt:
        log.warning("Ejecución interrumpida por el usuario; se guarda lo avanzado.")

    # Lo que no alcanzó a procesarse queda marcado, no se pierde en silencio
    pendientes = dnis[len(registros):]
    registros.extend(construir_registro(d, "PENDIENTE", None) for d in pendientes)

    df = transformar(registros)
    cargar(df, args.output)
    log.info("Resumen: %s", df["Estado"].value_counts().to_dict())
    log.info("Padrón guardado en %s (%d filas).", args.output, len(df))
    return 2 if pendientes else 0


if __name__ == "__main__":
    sys.exit(main())