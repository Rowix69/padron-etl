"""Pruebas unitarias del pipeline. No hacen llamadas reales a la API."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

import padron_etl as etl


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    """Anula time.sleep (rotación y backoff) para que los tests corran al instante."""
    monkeypatch.setattr(etl.time, "sleep", lambda _s: None)


def respuesta(status: int, json_data: dict | None = None) -> MagicMock:
    """Simula un requests.Response."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    return r


# ----------------------------------------------------------------------------
# Normalización
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("12345678", "12345678"),
        ("1234567", "01234567"),      # cero a la izquierda perdido por Excel
        ("123456", "00123456"),
        (12345678, "12345678"),       # entero
        (12345678.0, "12345678"),     # float, como lo lee Excel a veces
        ("12345678.0", "12345678"),
        (" 12 345 678 ", "12345678"),
        ("12-345-678", "12345678"),
    ],
)
def test_normalizar_dni_validos(entrada, esperado):
    assert etl.normalizar_dni(entrada) == esperado


@pytest.mark.parametrize(
    "entrada",
    [None, float("nan"), pd.NA, "", "ABC12345", "123456789", "12345"],
)
def test_normalizar_dni_invalidos(entrada):
    assert etl.normalizar_dni(entrada) is None


@pytest.mark.parametrize(
    "cabecera, esperado",
    [
        ("  DNI ", "dni"),
        ("N° DNI", "ndni"),
        ("Documento de Identidad", "documentodeidentidad"),
        ("Teléfono", "telefono"),
    ],
)
def test_normalizar_cabecera(cabecera, esperado):
    assert etl.normalizar_cabecera(cabecera) == esperado


# ----------------------------------------------------------------------------
# Detección dinámica de columna
# ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cabecera",
    ["dni", "DNI", "Dni", "Documento", "N° DNI", "  dni  ", "DNI del vecino"],
)
def test_detecta_columna_por_nombre_en_cualquier_posicion(cabecera):
    df = pd.DataFrame({"Nombre": ["x"], "Edad": ["30"], cabecera: ["12345678"]})
    assert etl.detectar_columna_dni(df) == cabecera


def test_detecta_columna_por_contenido_si_la_cabecera_no_se_reconoce():
    df = pd.DataFrame(
        {
            "Vecino": ["Ana", "Luis", "Rosa"],
            "Telefono": ["987654321"] * 3,  # 9 dígitos: no debe confundirse con DNI
            "Identificacion": ["12345678", "87654321", "1234567"],
        }
    )
    assert etl.detectar_columna_dni(df) == "Identificacion"


def test_error_claro_si_no_existe_columna_dni():
    df = pd.DataFrame({"Nombre": ["Ana", "Luis"], "Edad": ["30", "41"]})
    with pytest.raises(ValueError):
        etl.detectar_columna_dni(df)


# ----------------------------------------------------------------------------
# Extract (integración con un Excel temporal)
# ----------------------------------------------------------------------------
def test_extraer_dnis_normaliza_deduplica_y_descarta_invalidos(tmp_path):
    ruta = tmp_path / "lista.xlsx"
    pd.DataFrame(
        {
            "Vecino": ["Ana", "Luis", "Rosa", "Eva", "Pedro"],
            "Documento": [1234567, "12345678", "12345678", None, "ABC"],
        }
    ).to_excel(ruta, index=False)

    assert etl.extraer_dnis(ruta) == ["01234567", "12345678"]


def test_extraer_dnis_archivo_inexistente(tmp_path):
    with pytest.raises(FileNotFoundError):
        etl.extraer_dnis(tmp_path / "no_existe.xlsx")


# ----------------------------------------------------------------------------
# Tokens
# ----------------------------------------------------------------------------
def test_cargar_tokens_separa_y_limpia(monkeypatch):
    monkeypatch.setenv("DECOLECTA_TOKENS", " a , b,,c ,")
    assert etl.cargar_tokens() == ["a", "b", "c"]


def test_cargar_tokens_sin_variable_aborta(monkeypatch):
    monkeypatch.delenv("DECOLECTA_TOKENS", raising=False)
    with pytest.raises(SystemExit):
        etl.cargar_tokens()


def test_token_pool_rota_y_se_agota():
    pool = etl.TokenPool(["t1", "t2"])
    assert pool.actual == "t1"
    pool.rotar()
    assert pool.actual == "t2"
    pool.rotar()
    assert pool.agotado
    with pytest.raises(etl.TokensAgotadosError):
        _ = pool.actual


# ----------------------------------------------------------------------------
# Consulta a la API (rate limiting)
# ----------------------------------------------------------------------------
def test_consulta_exitosa():
    session = MagicMock()
    session.get.return_value = respuesta(200, {"first_name": "ANA"})

    estado, datos = etl.consultar_dni(session, "12345678", etl.TokenPool(["t1"]))

    assert estado == "OK"
    assert datos["first_name"] == "ANA"


def test_rota_token_ante_429_y_reintenta_el_mismo_dni():
    session = MagicMock()
    session.get.side_effect = [respuesta(429), respuesta(200, {"first_name": "ANA"})]

    estado, _ = etl.consultar_dni(session, "12345678", etl.TokenPool(["t1", "t2"]))

    assert estado == "OK"
    segunda_llamada = session.get.call_args_list[1]
    assert segunda_llamada.kwargs["headers"]["Authorization"] == "Bearer t2"


@pytest.mark.parametrize("codigo", [401, 403, 429])
def test_lanza_error_si_todos_los_tokens_fallan(codigo):
    session = MagicMock()
    session.get.return_value = respuesta(codigo)

    with pytest.raises(etl.TokensAgotadosError):
        etl.consultar_dni(session, "12345678", etl.TokenPool(["t1", "t2"]))


def test_dni_inexistente_no_consume_ni_rota_tokens():
    session = MagicMock()
    session.get.return_value = respuesta(404)
    pool = etl.TokenPool(["t1", "t2"])

    estado, datos = etl.consultar_dni(session, "12345678", pool)

    assert (estado, datos) == ("NO_ENCONTRADO", None)
    assert pool.actual == "t1"


def test_error_de_red_se_reintenta_y_luego_se_marca():
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError()

    estado, _ = etl.consultar_dni(session, "12345678", etl.TokenPool(["t1"]))

    assert estado == "ERROR_RED"
    assert session.get.call_count == etl.MAX_REINTENTOS


def test_error_500_no_se_confunde_con_no_encontrado():
    session = MagicMock()
    session.get.return_value = respuesta(500)

    estado, _ = etl.consultar_dni(session, "12345678", etl.TokenPool(["t1"]))

    assert estado == "ERROR_HTTP_500"
    assert session.get.call_count == etl.MAX_REINTENTOS


def test_error_500_transitorio_se_recupera():
    session = MagicMock()
    session.get.side_effect = [respuesta(500), respuesta(200, {"first_name": "ANA"})]

    estado, _ = etl.consultar_dni(session, "12345678", etl.TokenPool(["t1"]))

    assert estado == "OK"


# ----------------------------------------------------------------------------
# Transform
# ----------------------------------------------------------------------------
def test_construir_registro_arma_nombre_completo_si_falta():
    persona = {"first_last_name": "PEREZ", "second_last_name": "GARCIA", "first_name": "ANA"}
    registro = etl.construir_registro("12345678", "OK", persona)
    assert registro["Nombre_Completo"] == "PEREZ GARCIA ANA"


def test_transformar_ordena_sin_tildes_y_deja_fallos_al_final():
    registros = [
        etl.construir_registro("00000003", "NO_ENCONTRADO", None),
        etl.construir_registro("00000002", "OK", {"first_last_name": "Zapata", "first_name": "Luis"}),
        etl.construir_registro("00000001", "OK", {"first_last_name": "Álvarez", "first_name": "Ana"}),
    ]

    df = etl.transformar(registros)

    assert df["DNI"].tolist() == ["00000001", "00000002", "00000003"]
    assert not any(col.startswith("_") for col in df.columns)