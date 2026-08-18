"""Regresion del ensamblado de chunks de DeepSeek.

La secuencia de eventos es la CAPTURADA EN VIVO el 2026-08-17 instrumentando
el proxy en produccion con deepseek-reasoner y el prompt "Di exactamente:
Hola mundo hermoso". Se conserva textual -- incluidas las rarezas -- porque
el valor del test esta en que es lo que el servidor manda de verdad, no lo
que uno supondria que manda.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import _iter_content_chunks   # noqa: E402


class RespFalsa:
    """Imita lo unico que _iter_content_chunks usa: iter_lines() en bytes."""
    def __init__(self, eventos):
        self._eventos = eventos

    def iter_lines(self):
        for e in self._eventos:
            yield ("data:" + json.dumps(e)).encode()


# Pensamiento del reasoner (se descarta), y despues el contenido de verdad.
SECUENCIA_REAL = [
    {"v": {"response": {"fragments": [{"type": "THINK", "content": "We"}]}}},
    {"v": " should"}, {"v": " output"}, {"v": ' "'},
    {"v": "H"}, {"v": "ola"}, {"v": " mundo"}, {"v": '"'},
    {"p": "response/fragments/-1/elapsed_secs", "o": "SET", "v": [1]},
    # Abre el fragmento de CONTENIDO con su primer trozo.
    {"p": "response/fragments", "o": "APPEND", "v": [{"type": "TEXT", "content": "H"}]},
    # El trozo siguiente llega con ruta pero SIN `o`. Este es el evento que se
    # perdia: tiene `p`, asi que no es "continuacion sin ruta", y no dice
    # "APPEND", asi que la rama de contenido lo rechazaba.
    {"p": "response/fragments/-1/content", "v": "ola"},
    # Y de aca en mas, sin ruta ni op (optimizacion de diff).
    {"v": " mundo"}, {"v": " her"}, {"v": "mos"}, {"v": "o"},
    {"p": "response/status", "o": "SET", "v": "FINISHED"},
]


def test_no_se_pierde_el_trozo_con_ruta_y_sin_op():
    salida = "".join(_iter_content_chunks(RespFalsa(SECUENCIA_REAL)))
    assert salida == "Hola mundo hermoso", repr(salida)


def test_el_pensamiento_no_se_filtra_al_contenido():
    salida = "".join(_iter_content_chunks(RespFalsa(SECUENCIA_REAL)))
    for fuga in ("should", "output", "We"):
        assert fuga not in salida, f"se filtro el pensamiento: {salida!r}"


def test_un_set_no_se_trata_como_append():
    """SET significa REEMPLAZAR: aceptarlo como append duplicaria el texto."""
    eventos = [
        {"p": "response/fragments", "o": "APPEND", "v": [{"type": "TEXT", "content": "abc"}]},
        {"p": "response/fragments/-1/content", "o": "SET", "v": "abc"},
        {"p": "response/status", "o": "SET", "v": "FINISHED"},
    ]
    assert "".join(_iter_content_chunks(RespFalsa(eventos))) == "abc"


def test_sin_pensamiento_tambien_funciona():
    """deepseek-chat: no piensa, entra directo al contenido."""
    eventos = [
        {"p": "response/fragments", "o": "APPEND", "v": [{"type": "TEXT", "content": "Ho"}]},
        {"p": "response/fragments/-1/content", "v": "la"},
        {"v": " mundo"},
        {"p": "response/status", "o": "SET", "v": "FINISHED"},
    ]
    assert "".join(_iter_content_chunks(RespFalsa(eventos))) == "Hola mundo"
