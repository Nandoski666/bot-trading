#!/usr/bin/env python3
"""
T6 — Walk-forward analysis. Mide sobreajuste, que es la causa #1 de fracaso.

El problema que resuelve
------------------------
Un backtest optimizado siempre se ve bien. Esa es su naturaleza: se eligieron
los parametros *porque* se veian bien en esos datos. La pregunta util no es
"cuanto gano en el pasado" sino "cuanto de eso sobrevive en datos que el
optimizador no vio".

El metodo
---------
Ventanas rodantes de 12 meses de entrenamiento + 3 meses de prueba, avanzando
3 meses cada vez:

    train [------------ 12 meses ------------] test [-- 3 --]
                 train [------------ 12 meses ------------] test [-- 3 --]
                              train [---------- 12 meses ----------] test [...]

En cada ventana:
  1. se optimizan los parametros de senal SOLO sobre el tramo de entrenamiento
  2. se evalua con esos parametros congelados sobre el tramo de prueba
  3. se mide cuanto se degrado el resultado

Ademas se evalua la BASELINE de parametros fijos sobre el mismo tramo de
prueba. Esa comparacion responde a una pregunta que casi nadie se hace: si la
version optimizada no le gana a la de parametros por defecto en datos nuevos,
la optimizacion no estaba aprendiendo nada — estaba memorizando ruido.

Criterio de parada (definicion de hecho del ticket)
---------------------------------------------------
Si la degradacion media train -> test supera el 40 %, la estrategia esta
sobreajustada y NO avanza a dry-run.

Uso:
    python tools/walk_forward.py                  # ejecucion completa
    python tools/walk_forward.py --epochs 60      # busqueda mas larga
    python tools/walk_forward.py --solo-listar    # ver las ventanas y salir
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta

RAIZ = Path(__file__).resolve().parents[1]
DIR_ESTRATEGIAS = RAIZ / "user_data" / "strategies"
DIR_RESULTADOS = RAIZ / "user_data" / "backtest_results" / "walk_forward"

FREQTRADE = str(RAIZ / ".venv" / "bin" / "freqtrade")
if not Path(FREQTRADE).exists():
    FREQTRADE = "freqtrade"

# Costos: los mismos que T5. Una degradacion medida sin comisiones no dice nada
# sobre el sistema real.
COMISION_EFECTIVA = 0.0015

PARES = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT", "POL/USDT", "LTC/USDT", "ATOM/USDT"]
UMBRAL_DEGRADACION = 0.40   # definicion de hecho del ticket
SEMILLA = 42


# ---------------------------------------------------------------------------
# Definicion de las ventanas
# ---------------------------------------------------------------------------

@dataclass
class Ventana:
    indice: int
    train_desde: datetime
    train_hasta: datetime
    test_desde: datetime
    test_hasta: datetime

    @property
    def rango_train(self) -> str:
        return f"{self.train_desde:%Y%m%d}-{self.train_hasta:%Y%m%d}"

    @property
    def rango_test(self) -> str:
        return f"{self.test_desde:%Y%m%d}-{self.test_hasta:%Y%m%d}"

    def __str__(self) -> str:
        return (f"V{self.indice:02d}  train {self.train_desde:%Y-%m-%d}→{self.train_hasta:%Y-%m-%d}"
                f"  |  test {self.test_desde:%Y-%m-%d}→{self.test_hasta:%Y-%m-%d}")


def generar_ventanas(inicio: datetime, fin: datetime,
                     meses_train: int = 12, meses_test: int = 3,
                     meses_paso: int = 3) -> list[Ventana]:
    """Ventanas rodantes que cubren [inicio, fin] sin solapar los tramos de test.

    Los tramos de test son contiguos y disjuntos: encadenados forman una serie
    continua de resultados fuera de muestra, que es la curva de equity que se
    grafica al final.
    """
    ventanas: list[Ventana] = []
    train_desde = inicio
    i = 1
    while True:
        train_hasta = train_desde + relativedelta(months=meses_train)
        test_hasta = train_hasta + relativedelta(months=meses_test)
        if test_hasta > fin:
            break
        ventanas.append(Ventana(i, train_desde, train_hasta, train_hasta, test_hasta))
        train_desde += relativedelta(months=meses_paso)
        i += 1
    return ventanas


# ---------------------------------------------------------------------------
# Ejecucion de Freqtrade
# ---------------------------------------------------------------------------

def entorno() -> dict:
    """PYTHONPATH con el directorio de estrategias.

    El hyperopt reparte el trabajo en procesos hijo que vuelven a importar la
    estrategia desde cero. Sin esto, `reglas_riesgo` no se encuentra en el
    worker y el hyperopt muere al deserializar.
    """
    env = os.environ.copy()
    previo = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{DIR_ESTRATEGIAS}{os.pathsep}{previo}" if previo else str(DIR_ESTRATEGIAS)
    return env


def correr(comando: list[str], descripcion: str) -> subprocess.CompletedProcess:
    r = subprocess.run(comando, cwd=RAIZ, env=entorno(), text=True, capture_output=True)
    if r.returncode != 0:
        print(f"    FALLO en {descripcion} (codigo {r.returncode})", file=sys.stderr)
        cola = (r.stdout + r.stderr).splitlines()[-15:]
        print("    " + "\n    ".join(cola), file=sys.stderr)
    return r


def optimizar(ventana: Ventana, epochs: int, loss: str) -> dict | None:
    """Hyperopt sobre el tramo de entrenamiento. Devuelve los parametros hallados."""
    archivo_params = DIR_ESTRATEGIAS / "BaselineTrendOpt.json"
    archivo_params.unlink(missing_ok=True)   # nunca arrastrar los de la ventana previa

    comando = [
        FREQTRADE, "hyperopt",
        "--config", "user_data/config.dryrun.json",
        "--strategy", "BaselineTrendOpt",
        "--datadir", "user_data/data",
        "--timerange", ventana.rango_train,
        "--fee", str(COMISION_EFECTIVA),
        "--spaces", "buy",                 # SOLO senal. El riesgo no se optimiza.
        "--hyperopt-loss", loss,
        "--epochs", str(epochs),
        "--random-state", str(SEMILLA),
        "--job-workers", "4",
        "--pairs", *PARES,
    ]
    r = correr(comando, f"hyperopt ventana {ventana.indice}")
    if r.returncode != 0 or not archivo_params.exists():
        return None

    datos = json.loads(archivo_params.read_text())
    parametros = datos["params"]["buy"]

    # Copia archivada: sin esto no se puede reconstruir que se probo en cada
    # ventana, y el walk-forward deja de ser auditable.
    destino = DIR_RESULTADOS / f"params_V{ventana.indice:02d}.json"
    shutil.copy(archivo_params, destino)
    return parametros


def backtest(estrategia: str, timerange: str, etiqueta: str) -> dict | None:
    """Backtest de una estrategia sobre un rango. Devuelve las metricas."""
    directorio = DIR_RESULTADOS / etiqueta
    directorio.mkdir(parents=True, exist_ok=True)

    comando = [
        FREQTRADE, "backtesting",
        "--config", "user_data/config.dryrun.json",
        "--strategy", estrategia,
        "--datadir", "user_data/data",
        "--timerange", timerange,
        "--fee", str(COMISION_EFECTIVA),
        "--backtest-directory", str(directorio.relative_to(RAIZ)),
        "--cache", "none",
        "--pairs", *PARES,
    ]
    r = correr(comando, f"backtest {etiqueta}")
    if r.returncode != 0:
        return None
    return leer_metricas(directorio)


def leer_metricas(directorio: Path) -> dict | None:
    """Extrae las metricas del ultimo resultado guardado en `directorio`."""
    puntero = directorio / ".last_result.json"
    if not puntero.exists():
        return None
    nombre = json.loads(puntero.read_text())["latest_backtest"]
    with zipfile.ZipFile(directorio / nombre) as z:
        interno = next(n for n in z.namelist()
                       if n.endswith(".json") and "meta" not in n and "config" not in n)
        datos = json.loads(z.read(interno))

    est = next(iter(datos["strategy"].values()))
    operaciones = est.get("total_trades", 0)

    ganancias = sum(t["profit_abs"] for t in est["trades"] if t["profit_abs"] > 0)
    perdidas = -sum(t["profit_abs"] for t in est["trades"] if t["profit_abs"] < 0)

    if perdidas > 0:
        profit_factor = ganancias / perdidas
    elif ganancias > 0:
        profit_factor = float("inf")     # sin perdidas: sospechoso, no glorioso
    else:
        profit_factor = 0.0

    return {
        "operaciones": operaciones,
        # Los brutos se guardan porque la agregacion entre ventanas debe sumar
        # ganancias y perdidas, no promediar profit factors. Promediar
        # cocientes de muestras de tamano muy distinto da un numero sin sentido.
        "bruto_ganado": ganancias,
        "bruto_perdido": perdidas,
        "beneficio_pct": est.get("profit_total", 0.0) * 100,
        "profit_factor": profit_factor,
        "win_rate": (est.get("wins", 0) / operaciones * 100) if operaciones else 0.0,
        "max_drawdown_pct": est.get("max_drawdown_account", 0.0) * 100,
        "sharpe": est.get("sharpe", 0.0),
        "calmar": est.get("calmar", 0.0),
        "expectativa_pct": est.get("profit_mean", 0.0) * 100,
        "curva_equity": [
            {"fecha": t["close_date"], "beneficio_abs": t["profit_abs"]}
            for t in sorted(est["trades"], key=lambda x: x["close_date"])
        ],
    }


# ---------------------------------------------------------------------------
# Degradacion
# ---------------------------------------------------------------------------

def degradacion(train: dict, test: dict) -> float | None:
    """Degradacion relativa del profit factor de entrenamiento a prueba.

        degradacion = (PF_train - PF_test) / PF_train

    Se usa el profit factor y no el retorno porque el PF nunca es negativo, lo
    que hace que el cociente sea siempre interpretable. Un retorno de -3 % en
    train y -6 % en test daria una "degradacion del -100 %", que no significa
    nada.

    Devuelve None cuando el entrenamiento no produjo operaciones o su PF fue 0:
    en ese caso no hay nada de lo que degradarse y promediar un numero
    inventado ensuciaria el resultado global.
    """
    pf_train, pf_test = train["profit_factor"], test["profit_factor"]
    if pf_train in (0.0, float("inf")) or train["operaciones"] == 0:
        return None
    if pf_test == float("inf"):
        pf_test = pf_train
    return (pf_train - pf_test) / pf_train


def degradacion_agregada(resultados: list[Resultado]) -> dict | None:
    """Degradacion calculada sobre TODAS las operaciones juntas.

    Por que hace falta ademas de la media por ventana
    -------------------------------------------------
    La media de las degradaciones por ventana solo es informativa si cada
    ventana tiene operaciones suficientes. Con 2 o 3 operaciones en un tramo de
    prueba, su profit factor puede ser 0 o infinito por puro azar, y una sola
    ventana asi arrastra la media a cualquier sitio. En una ejecucion real de
    este proyecto aparecio una degradacion de -25.825 % en una ventana de 3
    operaciones: el numero es aritmeticamente correcto y no significa nada.

    La version agregada junta las ganancias brutas y las perdidas brutas de
    todos los tramos de entrenamiento por un lado, y de todos los de prueba por
    otro, y calcula UN profit factor de cada. Cada operacion pesa lo que pesa
    su tamano, no lo que pese la ventana donde cayo.

    Es la cifra sobre la que conviene decidir. La tabla por ventana sirve para
    ver *donde* se degrada, no *cuanto*.
    """
    g_train = sum(r.train["bruto_ganado"] for r in resultados
                  if r.train and "bruto_ganado" in r.train)
    p_train = sum(r.train["bruto_perdido"] for r in resultados
                  if r.train and "bruto_perdido" in r.train)
    g_test = sum(r.test["bruto_ganado"] for r in resultados
                 if r.test and "bruto_ganado" in r.test)
    p_test = sum(r.test["bruto_perdido"] for r in resultados
                 if r.test and "bruto_perdido" in r.test)

    ops_train = sum(r.train["operaciones"] for r in resultados if r.train)
    ops_test = sum(r.test["operaciones"] for r in resultados if r.test)

    if p_train <= 0 or ops_train == 0 or ops_test == 0:
        return None

    pf_train = g_train / p_train
    pf_test = g_test / p_test if p_test > 0 else float("inf")
    if pf_test == float("inf"):
        pf_test = pf_train

    return {
        "pf_train": pf_train,
        "pf_test": pf_test,
        "degradacion": (pf_train - pf_test) / pf_train,
        "ops_train": ops_train,
        "ops_test": ops_test,
    }


# ---------------------------------------------------------------------------
# Reporte
# ---------------------------------------------------------------------------

@dataclass
class Resultado:
    ventana: Ventana
    parametros: dict = field(default_factory=dict)
    train: dict | None = None
    test: dict | None = None
    baseline_test: dict | None = None

    @property
    def degradacion(self) -> float | None:
        if not (self.train and self.test):
            return None
        return degradacion(self.train, self.test)


def fmt(valor, sufijo: str = "", decimales: int = 2) -> str:
    if valor is None:
        return "—"
    if valor == float("inf"):
        return "∞"
    return f"{valor:.{decimales}f}{sufijo}"


def escribir_reporte(resultados: list[Resultado], destino: Path,
                     epochs: int, loss: str) -> tuple[float | None, bool]:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    degradaciones = [r.degradacion for r in resultados if r.degradacion is not None]
    media = sum(degradaciones) / len(degradaciones) if degradaciones else None

    agregada = degradacion_agregada(resultados)
    # El veredicto se toma sobre la cifra agregada, que es la unica robusta
    # cuando las ventanas individuales tienen pocas operaciones.
    principal = agregada["degradacion"] if agregada else media

    # --- La trampa del criterio de degradacion ------------------------------
    # Una degradacion baja solo significa algo si el entrenamiento produjo algo
    # que valiera la pena conservar. Un sistema con profit factor 0.42 dentro de
    # muestra y 0.36 fuera "solo se degrada un 14 %" — porque no se puede caer
    # mucho desde el suelo. Leer eso como aprobado seria dar luz verde a un
    # sistema que pierde dinero en las dos muestras.
    #
    # Por eso el criterio se declara NO APLICABLE cuando el entrenamiento ya es
    # perdedor. Un umbral de seguridad que da verde sobre un sistema roto es
    # peor que no tener umbral: transmite una confianza que no existe.
    entrenamiento_rentable = agregada is not None and agregada["pf_train"] > 1.0
    aplicable = agregada is not None and entrenamiento_rentable
    aprueba = aplicable and principal < UMBRAL_DEGRADACION

    L: list[str] = []
    a = L.append

    a("# Walk-forward analysis (T6)\n")
    a(f"*Generado: {ahora} — `python tools/walk_forward.py`*\n")
    a(f"- Ventanas: **12 meses de entrenamiento + 3 de prueba**, avanzando 3 meses")
    a(f"- Optimizacion: `{loss}`, {epochs} epochs, semilla {SEMILLA}, espacio `buy` unicamente")
    a(f"- Costos: **{COMISION_EFECTIVA:.2%} por lado** (0.10 % comision + 0.05 % slippage)")
    a(f"- Pares: {', '.join(f'`{p}`' for p in PARES)}\n")

    # --- Veredicto ---
    a("## Veredicto\n")
    if principal is None:
        a("**INDETERMINADO** — no hubo operaciones suficientes para medir nada.\n")
    elif not entrenamiento_rentable:
        a(f"**NO APLICABLE — y eso es peor que no pasar.**\n")
        a(f"La degradacion agregada es **{principal:.1%}**, por debajo del umbral "
          f"del {UMBRAL_DEGRADACION:.0%}. Tomada sola, esa cifra diria «pasa». "
          f"No lo hace, y conviene entender por que:\n")
        a(f"| | Operaciones | Profit factor |")
        a("|---|---:|---:|")
        a(f"| Entrenamiento | {agregada['ops_train']} | **{fmt(agregada['pf_train'])}** |")
        a(f"| Prueba | {agregada['ops_test']} | **{fmt(agregada['pf_test'])}** |")
        a("")
        a("**Los dos estan por debajo de 1.0: la estrategia pierde dinero dentro y "
          "fuera de muestra.** La degradacion es baja porque no se puede caer mucho "
          "desde el suelo, no porque el sistema generalice bien.\n")
        a("El criterio de degradacion mide *cuanto de lo aprendido sobrevive fuera "
          "de muestra*. Si no se aprendio nada rentable, la pregunta no tiene "
          "sentido — como medir la fidelidad de una copia de un original en blanco.\n")
        a("**Conclusion: la estrategia no avanza a dry-run.** No por sobreajuste, "
          "sino por algo mas basico: no funciona ni siquiera en los datos donde se "
          "ajustaron sus parametros.\n")
    elif aprueba:
        a(f"**PASA** — degradacion agregada train → test: **{principal:.1%}** "
          f"(umbral: {UMBRAL_DEGRADACION:.0%}), con un profit factor de "
          f"entrenamiento de {fmt(agregada['pf_train'])} (> 1.0, condicion previa "
          "para que la degradacion signifique algo).\n")
    else:
        a(f"**NO PASA** — degradacion agregada train → test: **{principal:.1%}**, "
          f"por encima del umbral del {UMBRAL_DEGRADACION:.0%}.\n")
        a("Segun la definicion de hecho del ticket T6, la estrategia esta "
          "sobreajustada y **no avanza a dry-run**.\n")

    if agregada and entrenamiento_rentable:
        a("### Como se calcula esta cifra\n")
        a("Sumando las ganancias y perdidas brutas de **todos** los tramos por "
          "separado, y comparando un unico profit factor de cada lado:\n")
        a("| | Operaciones | Profit factor |")
        a("|---|---:|---:|")
        a(f"| Entrenamiento (dentro de muestra) | {agregada['ops_train']} "
          f"| {fmt(agregada['pf_train'])} |")
        a(f"| Prueba (fuera de muestra) | {agregada['ops_test']} "
          f"| {fmt(agregada['pf_test'])} |")
        a("")
        a("No se promedian los profit factors de cada ventana: con tramos de "
          "prueba de 2 o 3 operaciones, un solo cociente extremo arrastra la media "
          "a cualquier sitio. Agregando, cada operacion pesa lo que pesa su "
          "resultado y no lo que pese la ventana donde cayo.\n")
        if media is not None:
            a(f"*(Para referencia, la media simple de las degradaciones por ventana "
              f"es {media:.1%}. Si difiere mucho de la agregada, es senal de que las "
              f"ventanas tienen tamanos muy dispares y la media no es de fiar.)*\n")

    # --- Tabla principal ---
    a("## Degradacion por ventana\n")
    a("| Ventana | Train | Test | Ops train | Ops test | PF train | PF test | Degradacion |")
    a("|---|---|---|---:|---:|---:|---:|---:|")
    for r in resultados:
        v = r.ventana
        tr, te = r.train, r.test
        deg = r.degradacion
        a(f"| V{v.indice:02d} "
          f"| {v.train_desde:%Y-%m} → {v.train_hasta:%Y-%m} "
          f"| {v.test_desde:%Y-%m} → {v.test_hasta:%Y-%m} "
          f"| {tr['operaciones'] if tr else '—'} "
          f"| {te['operaciones'] if te else '—'} "
          f"| {fmt(tr['profit_factor'] if tr else None)} "
          f"| {fmt(te['profit_factor'] if te else None)} "
          f"| {f'{deg:+.1%}' if deg is not None else '—'} |")
    a("")
    if media is not None:
        a(f"**Media simple: {media:.1%}** sobre {len(degradaciones)} ventanas medibles "
          f"de {len(resultados)}. *No es la cifra del veredicto* — ver arriba.\n")
        a("> Una degradacion positiva significa que el resultado empeoro fuera de la "
          "muestra: lo normal. Lo preocupante no es que baje, es cuanto. Una "
          "degradacion negativa en una ventana suelta es ruido, no una virtud.\n")

    # --- Optimizado vs baseline ---
    a("## Optimizado vs. baseline de parametros fijos\n")
    a("La pregunta que casi nadie se hace: en datos nuevos, ¿le gana la version "
      "optimizada a la de parametros por defecto? Si no le gana, la optimizacion "
      "no aprendio nada — memorizo ruido del tramo de entrenamiento.\n")
    a("| Ventana | Beneficio test (optimizado) | Beneficio test (baseline) | Diferencia |")
    a("|---|---:|---:|---:|")
    gana = 0
    comparables = 0
    for r in resultados:
        if not (r.test and r.baseline_test):
            continue
        comparables += 1
        opt, base = r.test["beneficio_pct"], r.baseline_test["beneficio_pct"]
        if opt > base:
            gana += 1
        a(f"| V{r.ventana.indice:02d} | {opt:+.2f} % | {base:+.2f} % | {opt - base:+.2f} pp |")
    a("")
    if comparables:
        a(f"La version optimizada gana a la baseline en **{gana} de {comparables}** ventanas "
          f"({gana / comparables:.0%}). Con un 50 % la optimizacion equivale a lanzar una "
          "moneda: no esta capturando nada estable.\n")

    # --- Parametros elegidos ---
    a("## Parametros elegidos en cada ventana\n")
    a("Si los parametros optimos saltan de una ventana a otra, no existe un "
      "optimo estable: el optimizador esta persiguiendo el ruido de cada periodo.\n")
    a("| Ventana | EMA rapida | EMA lenta | RSI min | RSI max |")
    a("|---|---:|---:|---:|---:|")
    for r in resultados:
        p = r.parametros or {}
        a(f"| V{r.ventana.indice:02d} | {p.get('ema_rapida', '—')} | {p.get('ema_lenta', '—')} "
          f"| {p.get('rsi_minimo', '—')} | {p.get('rsi_maximo', '—')} |")
    a("")
    if resultados and all(r.parametros for r in resultados):
        for clave, etiqueta in [("ema_rapida", "EMA rapida"), ("ema_lenta", "EMA lenta"),
                                ("rsi_minimo", "RSI min"), ("rsi_maximo", "RSI max")]:
            valores = [r.parametros[clave] for r in resultados]
            a(f"- **{etiqueta}**: recorrido {min(valores)}–{max(valores)}, "
              f"{len(set(valores))} valores distintos en {len(valores)} ventanas")
        a("")

    # --- Metricas completas del tramo de prueba ---
    a("## Metricas de los tramos de prueba (fuera de muestra)\n")
    a("| Ventana | Ops | Beneficio | Win rate | PF | Max DD | Sharpe | Calmar |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in resultados:
        t = r.test
        if not t:
            a(f"| V{r.ventana.indice:02d} | — | — | — | — | — | — | — |")
            continue
        a(f"| V{r.ventana.indice:02d} | {t['operaciones']} | {t['beneficio_pct']:+.2f} % "
          f"| {t['win_rate']:.1f} % | {fmt(t['profit_factor'])} | {t['max_drawdown_pct']:.2f} % "
          f"| {fmt(t['sharpe'])} | {fmt(t['calmar'])} |")
    a("")

    # --- Curva de equity concatenada ---
    total_ops = sum(r.test["operaciones"] for r in resultados if r.test)
    total_beneficio = sum(r.test["beneficio_pct"] for r in resultados if r.test)
    a("## Curva de equity concatenada de los tramos de prueba\n")
    a(f"Encadenando los {len([r for r in resultados if r.test])} tramos de prueba se "
      f"obtiene una serie continua **enteramente fuera de muestra**: "
      f"**{total_ops} operaciones**, retorno acumulado aproximado "
      f"**{total_beneficio:+.2f} %** (suma de retornos por ventana).\n")
    a("![Curva de equity walk-forward](walk_forward_equity.png)\n")

    # --- Como leer esto ---
    a("## Como leer este reporte\n")
    a("1. **Degradacion media < 40 %** es la condicion de paso del ticket. Por encima, "
      "los parametros estan ajustados al pasado y no al mercado.")
    a("2. **Estabilidad de los parametros** importa tanto como la degradacion. Cuatro "
      "ventanas con cuatro optimos distintos significan que no hay optimo.")
    a("3. **Optimizado vs. baseline** es la prueba mas dura. Si la optimizacion no gana "
      "de forma consistente en datos nuevos, sobra: usa los parametros fijos.")
    a("4. Un numero de operaciones bajo por ventana (< 30) hace que sus metricas sean "
      "ruido. Se reportan igualmente, pero no se decide sobre ellas.\n")

    destino.write_text("\n".join(L), encoding="utf-8")
    return principal, aprueba


def graficar_equity(resultados: list[Resultado], destino: Path) -> None:
    """Curva de equity concatenada de todos los tramos de prueba."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("  (matplotlib no disponible: se omite la grafica)")
        return

    fechas, equity, fronteras = [], [], []
    acumulado = 0.0
    for r in resultados:
        if not r.test:
            continue
        fronteras.append(r.ventana.test_desde)
        for op in r.test["curva_equity"]:
            acumulado += op["beneficio_abs"]
            fechas.append(datetime.fromisoformat(op["fecha"].replace("Z", "+00:00")))
            equity.append(acumulado)

    if not fechas:
        print("  (sin operaciones fuera de muestra: se omite la grafica)")
        return

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(fechas, equity, linewidth=1.6, color="#2e86de")
    ax.axhline(0, color="#576574", linewidth=1, linestyle="--")
    ax.fill_between(fechas, equity, 0, where=[e >= 0 for e in equity],
                    color="#10ac84", alpha=0.18, interpolate=True)
    ax.fill_between(fechas, equity, 0, where=[e < 0 for e in equity],
                    color="#ee5253", alpha=0.18, interpolate=True)

    # Lineas verticales en el arranque de cada tramo de prueba: dejan ver si el
    # deterioro se concentra en un periodo o esta repartido.
    for f in fronteras:
        ax.axvline(f, color="#c8d6e5", linewidth=0.8, zorder=0)

    ax.set_title("Walk-forward — equity concatenada de los tramos fuera de muestra\n"
                 "(comision 0.15 % por lado incluida)", fontsize=12)
    ax.set_ylabel("Beneficio acumulado (USDT)")
    ax.set_xlabel("Fecha")
    ax.grid(alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(destino, dpi=130)
    plt.close(fig)
    print(f"  grafica: {destino.relative_to(RAIZ)}")


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Walk-forward analysis de BaselineTrend")
    p.add_argument("--inicio", default="2021-01-01")
    p.add_argument("--fin", default=None, help="por defecto, hoy")
    p.add_argument("--meses-train", type=int, default=12)
    p.add_argument("--meses-test", type=int, default=3)
    p.add_argument("--meses-paso", type=int, default=3)
    p.add_argument("--epochs", type=int, default=40)
    p.add_argument("--loss", default="SharpeHyperOptLossDaily")
    p.add_argument("--solo-listar", action="store_true")
    p.add_argument("--solo-reporte", action="store_true",
                   help="regenera el reporte desde los resultados ya guardados, "
                        "sin volver a optimizar")
    args = p.parse_args()

    inicio = datetime.fromisoformat(args.inicio).replace(tzinfo=timezone.utc)
    fin = (datetime.fromisoformat(args.fin).replace(tzinfo=timezone.utc)
           if args.fin else datetime.now(timezone.utc))

    ventanas = generar_ventanas(inicio, fin, args.meses_train,
                                args.meses_test, args.meses_paso)
    if not ventanas:
        print("No hay datos suficientes para una sola ventana.", file=sys.stderr)
        return 1

    print("=" * 78)
    print(f"Walk-forward — {len(ventanas)} ventanas "
          f"({args.meses_train}m train + {args.meses_test}m test, paso {args.meses_paso}m)")
    print("=" * 78)
    for v in ventanas:
        print(" ", v)
    print()

    if args.solo_listar:
        return 0

    DIR_RESULTADOS.mkdir(parents=True, exist_ok=True)
    resultados: list[Resultado] = []

    if args.solo_reporte:
        # Reconstruye desde los .zip y los params ya guardados. Util para
        # cambiar como se presentan o se agregan las metricas sin repetir horas
        # de hyperopt — y para que el reporte sea auditable a posteriori.
        print("Regenerando el reporte desde los resultados guardados…\n")
        for v in ventanas:
            archivo_params = DIR_RESULTADOS / f"params_V{v.indice:02d}.json"
            parametros = (json.loads(archivo_params.read_text())["params"]["buy"]
                          if archivo_params.exists() else {})
            resultados.append(Resultado(
                ventana=v,
                parametros=parametros,
                train=leer_metricas(DIR_RESULTADOS / f"V{v.indice:02d}_train"),
                test=leer_metricas(DIR_RESULTADOS / f"V{v.indice:02d}_test"),
                baseline_test=leer_metricas(DIR_RESULTADOS / f"V{v.indice:02d}_baseline"),
            ))
        con_datos = sum(1 for r in resultados if r.test)
        if con_datos == 0:
            print("No hay resultados guardados. Corre el walk-forward completo primero.",
                  file=sys.stderr)
            return 1
        print(f"  {con_datos} ventanas con resultados guardados\n")
        return finalizar(resultados, args)

    for v in ventanas:
        print(f"[{v.indice}/{len(ventanas)}] {v}")

        print(f"    optimizando ({args.epochs} epochs)…", flush=True)
        parametros = optimizar(v, args.epochs, args.loss)
        if parametros is None:
            print("    sin resultado de optimizacion; se salta la ventana")
            resultados.append(Resultado(ventana=v))
            continue
        print(f"    parametros: {parametros}")

        # Con el archivo de parametros en su sitio, ambos backtests de
        # BaselineTrendOpt los cargan automaticamente.
        train = backtest("BaselineTrendOpt", v.rango_train, f"V{v.indice:02d}_train")
        test = backtest("BaselineTrendOpt", v.rango_test, f"V{v.indice:02d}_test")

        # Control: la baseline de parametros fijos sobre el MISMO tramo de prueba.
        base = backtest("BaselineTrend", v.rango_test, f"V{v.indice:02d}_baseline")

        r = Resultado(ventana=v, parametros=parametros,
                      train=train, test=test, baseline_test=base)
        resultados.append(r)

        if train and test:
            deg = r.degradacion
            print(f"    train: {train['operaciones']:>3} ops, PF {fmt(train['profit_factor'])}"
                  f"  |  test: {test['operaciones']:>3} ops, PF {fmt(test['profit_factor'])}"
                  f"  |  degradacion: {f'{deg:+.1%}' if deg is not None else '—'}")
        print()

    # Limpiar el archivo de parametros: dejarlo puesto haria que un backtest
    # posterior de BaselineTrendOpt usara en silencio los de la ultima ventana.
    (DIR_ESTRATEGIAS / "BaselineTrendOpt.json").unlink(missing_ok=True)

    return finalizar(resultados, args)


def finalizar(resultados: list[Resultado], args) -> int:
    """Escribe reporte, grafica y volcado JSON. Comun a los dos modos."""
    reporte = DIR_RESULTADOS / "WALK_FORWARD_REPORT.md"
    media, aprueba = escribir_reporte(resultados, reporte, args.epochs, args.loss)
    graficar_equity(resultados, DIR_RESULTADOS / "walk_forward_equity.png")

    (DIR_RESULTADOS / "walk_forward_datos.json").write_text(json.dumps(
        [{"ventana": str(r.ventana), "parametros": r.parametros,
          "train": {k: v for k, v in (r.train or {}).items() if k != "curva_equity"},
          "test": {k: v for k, v in (r.test or {}).items() if k != "curva_equity"},
          "baseline_test": {k: v for k, v in (r.baseline_test or {}).items()
                            if k != "curva_equity"},
          "degradacion": r.degradacion}
         for r in resultados], indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print("=" * 78)
    if media is None:
        print("Degradacion: INDETERMINADA")
    elif aprueba:
        print(f"Degradacion agregada train → test: {media:.1%} "
              f"(umbral {UMBRAL_DEGRADACION:.0%}) — PASA")
    else:
        print(f"Degradacion agregada train → test: {media:.1%} — NO PASA")
        print("Ver el veredicto completo en el reporte: una degradacion baja sobre "
              "un entrenamiento perdedor no es una aprobacion.")
    print(f"Reporte: {reporte.relative_to(RAIZ)}")
    return 0 if aprueba else 4


if __name__ == "__main__":
    raise SystemExit(main())
