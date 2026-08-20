"""
Utilidades compartidas por los tests.

Los tests de este proyecto no arrancan Freqtrade completo: construyen
dataframes sinteticos con la forma exacta que produce el motor y verifican la
logica de la estrategia contra ellos. Eso los hace correr en segundos y, sobre
todo, permite crear escenarios que en datos reales serian dificiles de aislar
(un cruce de EMAs en una vela conocida, un ATR concreto, una racha de perdidas).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# La estrategia vive fuera del sys.path habitual (Freqtrade la carga por ruta).
RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "user_data" / "strategies"))


# ---------------------------------------------------------------------------
# Generadores de datos sinteticos
# ---------------------------------------------------------------------------

def construir_ohlcv(precios: list[float] | np.ndarray,
                    volumenes: list[float] | np.ndarray | None = None,
                    inicio: datetime | None = None,
                    timeframe_horas: int = 1) -> pd.DataFrame:
    """Construye un OHLCV valido a partir de una serie de precios de cierre.

    Cada vela se genera con un rango pequeno y coherente alrededor del cierre,
    de modo que high >= max(open, close) y low <= min(open, close) siempre se
    cumplan. Los tests que necesiten un ATR concreto usan `construir_ohlcv_atr`.
    """
    precios = np.asarray(precios, dtype=float)
    n = len(precios)
    inicio = inicio or datetime(2024, 1, 1, tzinfo=timezone.utc)

    fechas = [inicio + timedelta(hours=timeframe_horas * i) for i in range(n)]
    apertura = np.concatenate([[precios[0]], precios[:-1]])
    maximo = np.maximum(apertura, precios) * 1.001
    minimo = np.minimum(apertura, precios) * 0.999

    if volumenes is None:
        volumenes = np.full(n, 1000.0)

    return pd.DataFrame({
        "date": pd.to_datetime(fechas, utc=True),
        "open": apertura,
        "high": maximo,
        "low": minimo,
        "close": precios,
        "volume": np.asarray(volumenes, dtype=float),
    })


def construir_ohlcv_atr(n: int, precio: float, rango: float,
                        inicio: datetime | None = None) -> pd.DataFrame:
    """OHLCV con precio plano y rango constante -> ATR converge a `rango`.

    Sirve para probar el dimensionamiento y el stop con un ATR conocido y
    exacto, sin depender de que un generador aleatorio produzca el valor
    esperado.
    """
    inicio = inicio or datetime(2024, 1, 1, tzinfo=timezone.utc)
    fechas = [inicio + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame({
        "date": pd.to_datetime(fechas, utc=True),
        "open": np.full(n, precio),
        "high": np.full(n, precio + rango / 2),
        "low": np.full(n, precio - rango / 2),
        "close": np.full(n, precio),
        "volume": np.full(n, 1000.0),
    })


def serie_con_cruce_alcista(velas_previas: int = 700, seed: int = 7) -> pd.DataFrame:
    """Serie disenada para producir UN cruce alcista de EMA(20) sobre EMA(50).

    Estructura:
      1. Fase alcista larga y suave que deja el precio por encima de la EMA(200).
      2. Correccion que mete la EMA(20) por debajo de la EMA(50).
      3. Rebote que la vuelve a cruzar por encima.

    Sobre la deriva se superpone una oscilacion de amplitud suficiente para que
    haya velas bajistas de verdad. Sin ella la serie seria monotona, el RSI se
    pegaria a 100 y ningun test que dependa del RSI significaria nada — un
    mercado que solo sube no es un caso de prueba, es un artefacto.

    Determinista: misma semilla, misma serie.
    """
    rng = np.random.default_rng(seed)
    amp = 0.02        # amplitud de la oscilacion (2 %)
    periodo = 5.0     # velas por ciclo -> genera velas rojas y verdes alternas

    def ondular(serie: np.ndarray) -> np.ndarray:
        t = np.arange(len(serie))
        return serie * (1 + amp * np.sin(t / periodo))

    base = ondular(100.0 * (1 + np.linspace(0, 0.60, velas_previas)))
    correccion = ondular(base[-1] * (1 - np.linspace(0, 0.08, 60)))
    rebote = ondular(correccion[-1] * (1 + np.linspace(0, 0.06, 45)))

    precios = np.concatenate([base, correccion, rebote])

    volumen = np.concatenate([
        1000.0 * (1 + 0.10 * rng.standard_normal(velas_previas)),
        np.full(60, 800.0),
        np.full(45, 1000.0),
    ])
    volumen[-12:] = 3500.0   # pico de participacion en el tramo del cruce
    return construir_ohlcv(precios, np.abs(volumen))


def indices_de_cruce(df: pd.DataFrame, alcista: bool = True) -> list[int]:
    """Indices donde ema_rapida cruza a ema_lenta, derivados de los datos.

    Los tests no deben codificar a mano "el cruce esta en la vela 782": si se
    ajusta el generador, el numero cambia y el test pasaria a comprobar otra
    cosa sin avisar. Se localiza el cruce desde los propios indicadores.
    """
    arriba = df["ema_rapida"] > df["ema_lenta"]
    if alcista:
        evento = arriba & ~arriba.shift(1, fill_value=False)
    else:
        evento = ~arriba & arriba.shift(1, fill_value=True)
    return [i for i in df.index[evento] if not pd.isna(df.loc[i, "ema_lenta"])]


def neutralizar_filtros(df: pd.DataFrame) -> pd.DataFrame:
    """Deja pasar los filtros 2, 3 y 4 para aislar la deteccion del cruce.

    Un test que mezcla las cuatro condiciones no dice cual fallo cuando falla.
    Aqui se anulan las otras tres —regimen, RSI y volumen— de modo que la unica
    condicion que decide sea el cruce de EMAs. Cada filtro tiene ademas su
    propio test que verifica que, por separado, si es vinculante.
    """
    df = df.copy()
    df["ema_regimen"] = df["close"] * 0.50   # precio muy por encima del regimen
    df["rsi"] = 55.0                          # centro de la banda [40, 70]
    df["volumen_sma"] = df["volume"] * 0.50  # volumen siempre por encima
    return df


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def estrategia():
    """Instancia de BaselineTrend con la configuracion minima que necesita.

    No se arranca el bot: solo se instancia la clase para poder llamar a
    populate_indicators / populate_entry_trend / custom_stoploss directamente.
    """
    from BaselineTrend import BaselineTrend

    config = {
        "stake_currency": "USDT",
        "stake_amount": "unlimited",
        "max_open_trades": 3,
        "dry_run": True,
        "timeframe": "1h",
        "runmode": "backtest",
        "exchange": {"name": "binance"},
    }
    s = BaselineTrend(config)
    s.dp = None
    s.wallets = None
    return s


@pytest.fixture
def metadata():
    return {"pair": "BTC/USDT"}


# ---------------------------------------------------------------------------
# Dobles de prueba para los callbacks (dp, wallets, Trade)
# ---------------------------------------------------------------------------

class DataProviderFalso:
    """Sustituto de `self.dp` que devuelve un dataframe fijo.

    Solo implementa `get_analyzed_dataframe`, que es lo unico que la estrategia
    usa. Levantar el DataProvider real exigiria un exchange y una cache de
    velas: mucho aparato para verificar aritmetica.
    """

    def __init__(self, dataframe: pd.DataFrame):
        self._df = dataframe

    def get_analyzed_dataframe(self, pair: str, timeframe: str):
        return self._df, None


class WalletsFalsas:
    """Sustituto de `self.wallets` con un equity fijo y conocido."""

    def __init__(self, equity: float):
        self._equity = equity

    def get_total_stake_amount(self) -> float:
        return self._equity


class TradeFalso:
    """Sustituto de `Trade` para probar `custom_stoploss` sin base de datos.

    Reproduce los cuatro atributos que la estrategia consulta y el par
    get/set_custom_data, que en Freqtrade persiste en SQLite.
    """

    def __init__(self, open_rate: float, open_date_utc: datetime,
                 max_rate: float | None = None, pair: str = "BTC/USDT"):
        self.pair = pair
        self.open_rate = open_rate
        self.open_date_utc = open_date_utc
        self.max_rate = max_rate if max_rate is not None else open_rate
        self._custom: dict = {}

    def get_custom_data(self, key: str, default=None):
        return self._custom.get(key, default)

    def set_custom_data(self, key: str, value) -> None:
        self._custom[key] = value


def estrategia_con_datos(estrategia, dataframe: pd.DataFrame, equity: float = 10_000.0):
    """Conecta una estrategia a un dataframe y un equity concretos."""
    estrategia.dp = DataProviderFalso(dataframe)
    estrategia.wallets = WalletsFalsas(equity)
    return estrategia
