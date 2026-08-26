"""La API: preguntar, subir una foto y mirar qué recuerda de ti.

**No hay estado en este proceso.** La primera versión guardaba las
conversaciones en un diccionario del módulo, y eso tenía dos problemas que son
el mismo: se perdían al reiniciar y no se compartían entre workers, así que la
segunda petición podía caer en otro proceso y no saber de qué se hablaba. Ahora
cada petición reconstruye el agente desde la base y lo suelta al terminar;
levantar un cliente y leer un SQLite cuesta menos que un round-trip a Groq, y a
cambio el servidor se puede replicar y reiniciar sin que nadie lo note.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agente import PROVEEDORES, Yachaq
import herramientas
import memoria

app = FastAPI(
    title="Yachaq",
    description="Agente de naturaleza del Ecuador. Identifica especies con el "
                "modelo de Riksi, consulta los registros reales de GBIF y "
                "recuerda lo que le cuentas.",
    version="0.2",
)


class Pregunta(BaseModel):
    mensaje: str
    conversacion: str | None = None
    # ponytail: quién eres es lo que digas que eres. Sin autenticación,
    # cualquiera que acierte un nombre lee esa memoria; para un servicio
    # publicado esto pasa a ser un token, y el cambio es una dependencia de
    # FastAPI, no un rediseño.
    usuario: str = "anonimo"


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
    }


@app.post("/preguntar")
def preguntar(pregunta: Pregunta):
    """Una pregunta en lenguaje natural. Devuelve la respuesta y, a la vista,
    qué herramientas usó el agente para llegar a ella."""
    clave = pregunta.conversacion or uuid.uuid4().hex[:12]
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
def ver_memoria(usuario: str):
    """Qué recuerda de alguien, con sus palabras.

    Una memoria que no se puede leer ni borrar es un problema, no una función:
    quien habla tiene derecho a ver qué se apuntó de él y a quitarlo.
    """
    return {"usuario": usuario, "recuerdos": memoria.recuerdos(usuario)}


@app.delete("/memoria/{usuario}")
def borrar_memoria(usuario: str):
    """Borra la memoria entera de alguien."""
    return {"usuario": usuario, **memoria.olvidar_todo(usuario)}
