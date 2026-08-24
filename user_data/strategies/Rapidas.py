"""
Variantes de 5 minutos — para ver operaciones rapido, no para ganar mas.

POR QUE EXISTEN
===============
En 1 hora, una operacion dura ~4 horas de mediana y pasan dias enteros sin que
salte una senal. Eso hace la validacion lenta y desesperante: no se sabe si el
sistema funciona o si esta parado.

Estas variantes usan el MISMO codigo de senal y el MISMO riesgo, pero sobre
velas de 5 minutos. Las operaciones duran minutos en vez de horas y se acumulan
decenas al dia, asi que en 48 horas se tiene mas muestra que en un mes con 1h.

LO QUE HAY QUE SABER ANTES DE USARLAS
=====================================
Bajar de 1h a 5m no es un ajuste neutro. Cambia la economia de la estrategia:

  * **Los costes no bajan.** Cada operacion completa cuesta 0.30 % del nocional,
    igual en 5m que en 1h. Pero el movimiento tipico de una vela de 5 minutos es
    mucho menor que el de una de 1 hora, asi que ese 0.30 % pasa de ser un peaje
    a ser el factor dominante.

  * **El ATR se encoge, el stop se acerca.** El stop es 2 x ATR; con velas de
    5 minutos el ATR es una fraccion del de 1h, asi que el stop queda muy cerca
    del precio de entrada y el ruido normal lo toca constantemente.

  * **El tamano de posicion crece.** El riesgo es 0.5 % del equity dividido por
    la distancia al stop. Stop mas cerca => posicion mas grande => mas nocional
    => mas comision en valor absoluto por cada 0.5 % arriesgado.

Los tres efectos empujan en la misma direccion. Por eso estas variantes se
miden antes de desplegarlas, y el resultado esta en
`user_data/backtest_results/comparativa/RAPIDAS.md`.

Sirven para **verificar que la maquinaria funciona** —que entra, que gestiona
el stop, que cierra, que reporta— con muchas muestras en poco tiempo. No para
concluir que la estrategia es buena: eso ya lo mide el backtest en 1h con anos
de datos.
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from BaselineTrend import BaselineTrend
from MomentumMultiple import MomentumMultiple
from Orochi import Orochi
from ReversionRSI import ReversionRSI
from RupturaDonchian import RupturaDonchian

# 5 minutos: 12 velas por hora.
TIMEFRAME_RAPIDO = "5m"

# Las mismas 600 velas de calentamiento, que en 5m son ~50 horas. Suficiente
# para que la EMA(200) converja (200 velas = 16.6 h) con margen de sobra.
CALENTAMIENTO_RAPIDO = 600

# Una senal caduca en 2 velas, igual que en 1h — pero eso ahora son 10 minutos
# y no 2 horas. Sin este ajuste, una senal de 5m seguiria viva 24 velas
# despues, cuando el mercado que la genero ya no existe.
CADUCIDAD_RAPIDA = 600   # segundos = 2 velas de 5m


class _Rapida:
    """Mezcla que convierte cualquier estrategia a 5 minutos.

    Va PRIMERO en la lista de bases para que sus atributos ganen sobre los de
    la estrategia original. No toca ninguna regla de riesgo: el
    dimensionamiento, el stop por ATR y el limite de posiciones siguen siendo
    los de EstrategiaBase, heredados sin modificar.
    """

    timeframe = TIMEFRAME_RAPIDO
    startup_candle_count: int = CALENTAMIENTO_RAPIDO
    ignore_buying_expired_candle_after = CADUCIDAD_RAPIDA


class BaselineTrendRapida(_Rapida, BaselineTrend):
    hipotesis = BaselineTrend.hipotesis + " (evaluada en velas de 5 minutos)"


class OrochiRapida(_Rapida, Orochi):
    hipotesis = Orochi.hipotesis + " (evaluada en velas de 5 minutos)"

    # El perfil de volumen se define por una ventana de tiempo, no de velas.
    # 168 velas de 1h son una semana; 168 velas de 5m son 14 horas, que no es
    # una subasta comparable. Se reescala para conservar el horizonte.
    pass


class ReversionRSIRapida(_Rapida, ReversionRSI):
    hipotesis = ReversionRSI.hipotesis + " (evaluada en velas de 5 minutos)"


class RupturaDonchianRapida(_Rapida, RupturaDonchian):
    hipotesis = RupturaDonchian.hipotesis + " (evaluada en velas de 5 minutos)"


class MomentumMultipleRapida(_Rapida, MomentumMultiple):
    hipotesis = MomentumMultiple.hipotesis + " (evaluada en velas de 5 minutos)"
