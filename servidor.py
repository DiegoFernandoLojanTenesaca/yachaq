"""La API: preguntar, subir una foto y mirar qué recuerda de ti.

**No hay estado en este proceso.** La primera versión guardaba las
conversaciones en un diccionario del módulo, y eso tenía dos problemas que son
el mismo: se perdían al reiniciar y no se compartían entre workers, así que la
segunda petición podía caer en otro proceso y no saber de qué se hablaba. Ahora
cada petición reconstruye el agente desde la base y lo suelta al terminar;
levantar un cliente y leer un SQLite cuesta menos que un round-trip a Groq, y a
cambio el servidor se puede replicar y reiniciar sin que nadie lo note.
"""

import os
import shutil
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import FastAPI, File, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agente import PROVEEDORES, Yachaq
import equipo
import herramientas
import memoria

app = FastAPI(
    title="Yachaq",
    description="Agente de naturaleza del Ecuador. Identifica especies con el "
                "modelo de Riksi, consulta los registros reales de GBIF y "
                "recuerda lo que le cuentas.",
    version="0.3",
)

# De dónde se acepta que llamen. El chat de Riksi vive en otro dominio, así que
# sin esto el navegador bloquea la petición antes de enviarla. Se listan los
# orígenes en vez de poner "*": con "*" cualquier página puede montar un chat
# encima de esta API y gastar la cuota, y las cuentas gratuitas se agotan.
ORIGENES = [o.strip() for o in os.environ.get(
    "YACHAQ_ORIGENES",
    "https://diegofernandolojantenesaca.github.io,http://localhost:8080,"
    "http://127.0.0.1:8080").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

# Cuántas preguntas por IP y por hora. Cada una gasta cuota de un proveedor
# gratuito, así que sin tope una sola persona -o un bot- deja el chat inservible
# para el resto. En memoria y no en la base: si el proceso se reinicia se
# perdona a todo el mundo, y para esto eso es aceptable.
POR_HORA = int(os.environ.get("YACHAQ_POR_HORA", "20"))

# **Leer la memoria de alguien exige la llave; usarla, no.** Cualquiera puede
# preguntar y el agente recordará lo que le cuenten bajo el nombre que envíen,
# pero `GET /memoria/{usuario}` deja ver esos recuerdos y `DELETE` los borra, y
# eso con solo acertar un nombre es demasiado.
#
# No es autenticación de verdad -no hay cuentas, y no las hace falta todavía-,
# es cerrar dos endpoints que no deberían estar abiertos en un servicio
# publicado. Sin `YACHAQ_LLAVE` configurada quedan abiertos, que es lo cómodo en
# local; en producción se pone y ya está.
LLAVE = os.environ.get("YACHAQ_LLAVE", "")


def _con_llave(dada: str | None) -> bool:
    """Compara en tiempo constante: comparar con `==` filtra la longitud."""
    import secrets
    return not LLAVE or (dada is not None and secrets.compare_digest(dada, LLAVE))
_visitas: dict[str, deque] = {}


def _pasa_el_limite(peticion: Request) -> bool:
    ip = (peticion.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (peticion.client.host if peticion.client else "?"))
    ahora = time.time()
    cola = _visitas.setdefault(ip, deque())
    while cola and ahora - cola[0] > 3600:
        cola.popleft()
    if len(cola) >= POR_HORA:
        return False
    cola.append(ahora)

    # Sin esto, el diccionario crece con cada IP que pase por aquí.
    if len(_visitas) > 5000:
        for k in [k for k, v in _visitas.items() if not v or ahora - v[-1] > 3600]:
            _visitas.pop(k, None)
    return True


class Pregunta(BaseModel):
    mensaje: str
    conversacion: str | None = None
    # ponytail: quién eres es lo que digas que eres. Sin autenticación,
    # cualquiera que acierte un nombre lee esa memoria; para un servicio
    # publicado esto pasa a ser un token, y el cambio es una dependencia de
    # FastAPI, no un rediseño.
    usuario: str = "anonimo"
    # Repartir cuesta dos llamadas de más al modelo, así que no se hace siempre:
    # con `equipo`, el coordinador decide si la pregunta lo merece.
    equipo: bool = False


@app.get("/salud")
def salud():
    """Sirve para saber si el contenedor está vivo y con qué está hablando."""
    bd = memoria._bd()
    return {
        "estado": "vivo",
        "proveedores": list(PROVEEDORES),
        "herramientas": [e["function"]["name"] for e in herramientas.esquemas()],
        "conversaciones_guardadas": bd.execute("SELECT count(*) FROM conversaciones").fetchone()[0],
        "recuerdos_guardados": bd.execute("SELECT count(*) FROM recuerdos").fetchone()[0],
        "origenes_permitidos": ORIGENES,
        "preguntas_por_hora": POR_HORA,
        "memoria_protegida": bool(LLAVE),
    }


@app.post("/preguntar")
def preguntar(pregunta: Pregunta, peticion: Request):
    """Una pregunta en lenguaje natural. Devuelve la respuesta y, a la vista,
    qué herramientas usó el agente para llegar a ella."""
    if not _pasa_el_limite(peticion):
        return JSONResponse(status_code=429, content={
            "respuesta": f"Has hecho {POR_HORA} preguntas en la última hora, que "
                         f"es el tope. La cuota es de un plan gratuito y se acaba. "
                         f"Vuelve en un rato; el identificador de la cámara sigue "
                         f"funcionando, que ese va dentro de tu navegador.",
            "herramientas": []})

    clave = pregunta.conversacion or uuid.uuid4().hex[:12]
    if pregunta.equipo:
        return {"conversacion": clave,
                **equipo.responder(pregunta.mensaje, pregunta.usuario, clave)}
    agente = Yachaq(usuario=pregunta.usuario, conversacion=clave)
    return {"conversacion": clave, **agente.responder(pregunta.mensaje)}


@app.post("/identificar")
async def identificar(foto: UploadFile = File(...), conversacion: str | None = None,
                      usuario: str = "anonimo"):
    """Sube una foto y pregunta por ella en la misma llamada.

    El archivo se guarda en un temporal y se borra al terminar: no hay motivo
    para acumular las fotos de nadie en el disco del servidor.
    """
    sufijo = Path(foto.filename or "foto.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        shutil.copyfileobj(foto.file, tmp)
        ruta = tmp.name

    try:
        clave = conversacion or uuid.uuid4().hex[:12]
        agente = Yachaq(usuario=usuario, conversacion=clave)
        salida = agente.responder(
            f"Identifica la foto que está en {ruta} y cuéntame qué es y dónde vive.")
        return {"conversacion": clave, **salida}
    except Exception as err:
        return JSONResponse(status_code=500, content={"error": f"{type(err).__name__}: {err}"})
    finally:
        Path(ruta).unlink(missing_ok=True)


@app.delete("/conversacion/{clave}")
def olvidar_conversacion(clave: str):
    """Cierra una conversación y borra su historia."""
    return {"olvidada": memoria.cerrar_conversacion(clave)}


@app.get("/memoria/{usuario}")
def ver_memoria(usuario: str, llave: str | None = Header(default=None,
                                                         alias="X-Yachaq-Llave")):
    """Qué recuerda de alguien, con sus palabras.

    Una memoria que no se puede leer ni borrar es un problema, no una función:
    quien habla tiene derecho a ver qué se apuntó de él y a quitarlo. Pero en un
    servicio publicado eso no puede depender de acertar un nombre.
    """
    if not _con_llave(llave):
        return JSONResponse(status_code=401, content={
            "error": "Esto necesita la llave, en la cabecera X-Yachaq-Llave."})
    return {"usuario": usuario, "recuerdos": memoria.recuerdos(usuario)}


@app.delete("/memoria/{usuario}")
def borrar_memoria(usuario: str, llave: str | None = Header(default=None,
                                                            alias="X-Yachaq-Llave")):
    """Borra la memoria entera de alguien."""
    if not _con_llave(llave):
        return JSONResponse(status_code=401, content={
            "error": "Esto necesita la llave, en la cabecera X-Yachaq-Llave."})
    return {"usuario": usuario, **memoria.olvidar_todo(usuario)}
