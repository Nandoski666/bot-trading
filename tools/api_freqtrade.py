"""
Cliente minimo de la API REST de Freqtrade.

Lo usan `kill_switch.py` y `watchdog.py`. Se habla con el bot por su API y no
tocando su base de datos directamente, por una razon concreta: la base de datos
es el estado *del bot*, y escribir en ella por detras mientras el bot corre
produce estados incoherentes. La API es la unica forma segura de darle ordenes.

No se usa ninguna libreria adicional a proposito: el modulo `requests` ya viene
con Freqtrade, y estas herramientas tienen que poder correr en una emergencia
sin instalar nada.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parents[1]


def cargar_env(ruta: Path | None = None) -> dict[str, str]:
    """Lee el .env sin dependencias externas.

    No sobrescribe variables que ya existan en el entorno: si alguien exporta
    una credencial a mano para una operacion puntual, esa gana.
    """
    ruta = ruta or (RAIZ / ".env")
    valores: dict[str, str] = {}
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, _, valor = linea.partition("=")
            valores[clave.strip()] = valor.strip().strip('"').strip("'")
    for clave, valor in valores.items():
        os.environ.setdefault(clave, valor)
    return valores


class ErrorAPI(RuntimeError):
    pass


class ClienteFreqtrade:
    """Envoltura sobre la API REST del bot."""

    def __init__(self, url: str = "http://127.0.0.1:8080",
                 usuario: str | None = None, clave: str | None = None,
                 timeout: int = 15):
        cargar_env()
        self.url = url.rstrip("/") + "/api/v1"
        self.timeout = timeout
        self.auth = (
            usuario or os.environ.get("FREQTRADE__API_SERVER__USERNAME", "freqtrader"),
            clave or os.environ.get("FREQTRADE__API_SERVER__PASSWORD", ""),
        )

    def _peticion(self, metodo: str, ruta: str, **kwargs):
        try:
            r = requests.request(metodo, f"{self.url}/{ruta}", auth=self.auth,
                                 timeout=self.timeout, **kwargs)
        except requests.exceptions.ConnectionError as exc:
            raise ErrorAPI(
                f"No se pudo conectar con el bot en {self.url}. "
                "¿Esta corriendo? ¿Esta habilitado api_server en el config?"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ErrorAPI(f"El bot no respondio en {self.timeout} s.") from exc

        if r.status_code == 401:
            raise ErrorAPI(
                "Credenciales de la API rechazadas. Revisa "
                "FREQTRADE__API_SERVER__USERNAME y __PASSWORD en .env."
            )
        if not r.ok:
            raise ErrorAPI(f"{metodo} {ruta} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    # --- Lectura ---------------------------------------------------------
    def ping(self) -> dict:
        return self._peticion("GET", "ping")

    def salud(self) -> dict:
        """Ultimo ciclo procesado por el bot. Es la base del heartbeat.

        `ping` solo dice que el servidor web responde; el bot puede estar vivo
        como proceso y con el hilo de trading bloqueado. `health` devuelve
        cuando termino el ultimo ciclo de verdad.
        """
        return self._peticion("GET", "health")

    def estado_bot(self) -> dict:
        return self._peticion("GET", "show_config")

    def posiciones_abiertas(self) -> list[dict]:
        return self._peticion("GET", "status")

    def balance(self) -> dict:
        return self._peticion("GET", "balance")

    def beneficio(self) -> dict:
        return self._peticion("GET", "profit")

    def resumen_diario(self, dias: int = 7) -> dict:
        return self._peticion("GET", "daily", params={"timescale": dias})

    def operaciones_cerradas(self, limite: int = 500) -> dict:
        return self._peticion("GET", "trades", params={"limit": limite})

    # --- Escritura -------------------------------------------------------
    def detener(self) -> dict:
        """Deja de abrir posiciones nuevas. Las abiertas se siguen gestionando."""
        return self._peticion("POST", "stop")

    def arrancar(self) -> dict:
        return self._peticion("POST", "start")

    def cerrar_posicion(self, id_operacion: int) -> dict:
        return self._peticion("POST", "forceexit", json={"tradeid": str(id_operacion),
                                                         "ordertype": "market"})

    def cerrar_todo(self) -> dict:
        return self._peticion("POST", "forceexit", json={"tradeid": "all",
                                                         "ordertype": "market"})
