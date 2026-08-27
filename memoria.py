"""Lo que Yachaq sigue sabiendo después de reiniciar.

Son dos cosas distintas y conviene no mezclarlas:

- **La conversación**: el ir y venir de una sesión. Se guarda entera y se
  recupera por su identificador. Sirve para que «¿y dónde vive?» sepa de qué
  animal se está hablando.
- **Los recuerdos**: hechos sueltos sobre quien pregunta —qué le interesa, por
  dónde sale al campo— que valen en *cualquier* conversación futura. Son pocos
  y caben enteros en el prompt.

Guardar la conversación entera como si fuera memoria a largo plazo no funciona:
crece sin tope y llena el contexto de charla. Por eso los recuerdos los extrae
el propio modelo con la herramienta `recordar`, cuando oye algo que merece
sobrevivir a la sesión.

**Aquí no hay Postgres, y esta vez tampoco.** El plan lo prometía para esta
fase. Al mirarlo de cerca, SQLite está en la biblioteca estándar, escribe con
WAL y aguanta de sobra un servidor con un proceso: meter un contenedor y un
esquema encima sería pagar por nada. Postgres gana el día que haya varias
réplicas del servidor escribiendo a la vez, porque SQLite no comparte fichero
entre máquinas. Ese día se cambia este módulo y nada más.
"""

import json
import os
import sqlite3
import threading
import time
from contextvars import ContextVar
from pathlib import Path

AQUI = Path(__file__).parent
BASE = Path(os.environ.get("YACHAQ_MEMORIA", AQUI / "memoria.db"))

# Los recuerdos van en el prompt del sistema de cada conversación, así que su
# número es dinero y ruido. Cuarenta frases cortas son unos 600 tokens.
TOPE = 40

# Dos recuerdos por encima de este parecido son el mismo dicho de otra forma.
# Sin esto, «me gustan los colibríes» y «me interesan mucho los colibríes»
# ocupan dos sitios y el prompt se llena de repeticiones.
DUPLICADO = 0.85

TIPOS = ("interes", "lugar", "dato")

ESQUEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS recuerdos (
    id      INTEGER PRIMARY KEY,
    usuario TEXT NOT NULL,
    tipo    TEXT NOT NULL,
    texto   TEXT NOT NULL,
    cuando  REAL NOT NULL,
    UNIQUE(usuario, texto)
);
CREATE TABLE IF NOT EXISTS conversaciones (
    clave       TEXT PRIMARY KEY,
    usuario     TEXT NOT NULL,
    historia    TEXT NOT NULL,
    actualizada REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS i_recuerdos ON recuerdos(usuario, cuando DESC);
"""

# Quién está preguntando. No es un argumento de las herramientas a propósito:
# si lo fuera, el modelo podría escribir cualquier nombre y leer la memoria de
# otro. Lo fija el agente antes de cada turno y las herramientas lo leen.
USUARIO = ContextVar("usuario", default="anonimo")

_candado = threading.Lock()

# Una conexión por hilo, no una compartida. Con `check_same_thread=False` y una
# sola conexión, los tres ayudantes del multi-agente leían a la vez sobre el
# mismo cursor y sqlite3 devolvía «bad parameter or other API misuse»: un fallo
# que no aparece hasta que hay concurrencia de verdad, porque el candado
# protegía las escrituras y las lecturas iban sueltas.
#
# Poner el candado también en las lecturas habría serializado justo lo que se
# paraleliza. Una conexión por hilo no comparte nada, y WAL deja que varios
# lectores y un escritor convivan sin bloquearse.
_local = threading.local()


def _bd():
    cx = getattr(_local, "cx", None)
    if cx is None:
        cx = _local.cx = sqlite3.connect(BASE)
        cx.row_factory = sqlite3.Row
        cx.executescript(ESQUEMA)
    return cx


def _escribir(sql, *args):
    # El candado se queda solo para escribir: WAL admite un escritor a la vez, y
    # sin él dos hilos guardando recuerdos chocan con «database is locked».
    with _candado:
        cx = _bd()
        cur = cx.execute(sql, args)
        cx.commit()
        return cur


# ── recuerdos ───────────────────────────────────────────────────────────────

def recuerdos(usuario=None, tope=TOPE):
    """Lo que se sabe de alguien, de lo más reciente a lo más antiguo."""
    filas = _bd().execute(
        "SELECT id, tipo, texto, cuando FROM recuerdos WHERE usuario=? "
        "ORDER BY cuando DESC LIMIT ?", (usuario or USUARIO.get(), tope)).fetchall()
    return [dict(f) for f in filas]


def _parecidos(texto, entre):
    """Cuánto se parece `texto` a cada uno de `entre`, por significado.

    Reusa el codificador del RAG: ya está cargado en memoria y evita que
    «salgo por el Cajas» y «suelo ir al Cajas» cuenten como dos cosas.
    """
    if not entre:
        return []
    import indice
    v = indice.vectorizar([texto] + entre)
    return (v[1:] @ v[0]).tolist()


def guardar(texto, tipo="dato", usuario=None):
    """Añade un recuerdo, salvo que ya hubiera uno que dice lo mismo."""
    usuario = usuario or USUARIO.get()
    texto = " ".join(texto.split())
    if not texto:
        return {"guardado": False, "motivo": "el hecho venía vacío"}
    if tipo not in TIPOS:
        tipo = "dato"

    ya = recuerdos(usuario, tope=200)
    for r, p in zip(ya, _parecidos(texto, [r["texto"] for r in ya])):
        if p >= DUPLICADO:
            return {"guardado": False, "motivo": "ya lo sabía",
                    "parecido_a": r["texto"], "parecido": round(p, 3)}

    _escribir("INSERT OR REPLACE INTO recuerdos(usuario, tipo, texto, cuando) "
              "VALUES(?,?,?,?)", usuario, tipo, texto, time.time())
    return {"guardado": True, "texto": texto, "tipo": tipo}


def borrar(sobre, usuario=None):
    """Borra el recuerdo que más se parezca a lo que se pide olvidar.

    Por texto libre y no por número: quien habla dice «olvida lo de los
    colibríes», no «borra el recuerdo 7». Se devuelve lo borrado para que el
    agente lo confirme y se vea si se llevó por delante lo que no era.
    """
    usuario = usuario or USUARIO.get()
    ya = recuerdos(usuario, tope=200)
    if not ya:
        return {"borrado": False, "motivo": "no había nada guardado"}

    parecidos = _parecidos(sobre, [r["texto"] for r in ya])
    mejor = max(range(len(ya)), key=lambda i: parecidos[i])
    if parecidos[mejor] < 0.44:          # el mismo corte medido del RAG
        return {"borrado": False, "motivo": "ningún recuerdo se parece a eso",
                "guardados": [r["texto"] for r in ya]}

    _escribir("DELETE FROM recuerdos WHERE id=?", ya[mejor]["id"])
    return {"borrado": True, "texto": ya[mejor]["texto"],
            "parecido": round(parecidos[mejor], 3)}


def olvidar_todo(usuario):
    """Borra la memoria entera de alguien. Sin esto, la memoria es una jaula."""
    return {"borrados": _escribir("DELETE FROM recuerdos WHERE usuario=?", usuario).rowcount}


def recordatorio(usuario=None):
    """Los recuerdos convertidos en el trozo de prompt que los lleva.

    Va marcado como recordado y no como consultado: es lo mismo que se le exige
    al agente cuando responde, y el prompt no puede predicar una cosa y hacer
    otra.
    """
    ya = recuerdos(usuario)
    if not ya:
        return ""
    lista = "\n".join(f"- ({r['tipo']}) {r['texto']}" for r in ya)
    return ("\n\nESTO TE LO CONTÓ ESTA PERSONA EN CONVERSACIONES ANTERIORES:\n"
            f"{lista}\n"
            "Úsalo para responder a su medida. No son datos consultados: si "
            "alguno resulta relevante, di que te lo contó, no lo presentes como "
            "un hecho comprobado.")


# ── conversaciones ──────────────────────────────────────────────────────────

def cargar_conversacion(clave):
    """La historia de una conversación, o None si no existe."""
    f = _bd().execute("SELECT usuario, historia FROM conversaciones WHERE clave=?",
                      (clave,)).fetchone()
    return (f["usuario"], json.loads(f["historia"])) if f else None


def guardar_conversacion(clave, usuario, historia):
    _escribir("INSERT OR REPLACE INTO conversaciones(clave, usuario, historia, actualizada) "
              "VALUES(?,?,?,?)", clave, usuario, json.dumps(historia, ensure_ascii=False),
              time.time())


def cerrar_conversacion(clave):
    return _escribir("DELETE FROM conversaciones WHERE clave=?", clave).rowcount > 0


def prueba():
    """Lo único con lógica propia: que no duplique, que borre lo que toca."""
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    global BASE
    BASE = AQUI / "memoria-prueba.db"
    BASE.unlink(missing_ok=True)
    _local.__dict__.pop("cx", None)

    u = "prueba"
    assert guardar("Me interesan sobre todo los colibríes", "interes", u)["guardado"]
    assert guardar("Salgo al campo por el Cajas", "lugar", u)["guardado"]

    # Lo mismo dicho de otra forma no ocupa un sitio nuevo: si lo hiciera, tras
    # veinte conversaciones el prompt sería la misma frase repetida.
    r = guardar("Me gustan mucho los colibríes", "interes", u)
    assert not r["guardado"], r
    assert len(recuerdos(u)) == 2, recuerdos(u)

    # Y algo distinto sí entra, aunque hable del mismo tema.
    assert guardar("Prefiere salir de madrugada", "dato", u)["guardado"]
    assert len(recuerdos(u)) == 3

    assert "Cajas" in recordatorio(u) and "conversaciones anteriores" in recordatorio(u).lower()

    b = borrar("olvida lo de los colibríes", u)
    assert b["borrado"] and "colibrí" in b["texto"], b
    assert len(recuerdos(u)) == 2

    # Borrar por texto libre tiene que fallar en silencio antes que llevarse el
    # recuerdo equivocado.
    assert not borrar("el precio del cobre en 1998", u)["borrado"]

    guardar_conversacion("c1", u, [{"role": "user", "content": "hola"}])
    usuario, historia = cargar_conversacion("c1")
    assert usuario == u and historia[0]["content"] == "hola"
    assert cerrar_conversacion("c1") and cargar_conversacion("c1") is None

    assert olvidar_todo(u)["borrados"] == 2 and not recuerdos(u)

    # Concurrencia: los tres ayudantes del multi-agente leen y escriben a la vez.
    # Con una conexión compartida esto reventaba con «bad parameter or other API
    # misuse», y el fallo no salía en ninguna prueba de un solo hilo.
    import concurrent.futures as futuros

    def trajinar(n):
        try:
            guardar(f"le interesa el tema numero {n}", "dato", u)
            return len(recuerdos(u))
        finally:
            # Cada hilo abre su conexión y hay que cerrarla: en Windows, un
            # fichero con conexiones vivas no se deja borrar al final.
            if getattr(_local, "cx", None):
                _local.cx.close()
                del _local.cx

    with futuros.ThreadPoolExecutor(6) as piscina:
        assert all(isinstance(x, int) for x in piscina.map(trajinar, range(6)))
    assert len(recuerdos(u)) == 6, recuerdos(u)
    olvidar_todo(u)

    print(f"ok · no duplica lo mismo dicho de otra forma · borra por texto libre "
          f"· la conversación sobrevive · aguanta 6 hilos a la vez · {BASE.name}")
    _bd().close()
    _local.__dict__.pop("cx", None)
    BASE.unlink(missing_ok=True)
    Path(str(BASE) + "-wal").unlink(missing_ok=True)
    Path(str(BASE) + "-shm").unlink(missing_ok=True)


if __name__ == "__main__":
    prueba()
