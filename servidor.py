"""La API: un endpoint de conversación y otro para subir una foto.

Cada conversación guarda su historia en memoria bajo un identificador. Es lo
que hace falta para que el agente recuerde de qué se estaba hablando dentro de
una sesión; la memoria que sobrevive al reinicio es la fase 4 y va a base de
datos, no aquí.
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

app = FastAPI(
    title="Yachaq",
    description="Agente de naturaleza del Ecuador. Identifica especies con el "
                "modelo de Riksi y consulta los registros reales de GBIF.",
    version="0.1",
)

# ponytail: las conversaciones viven en memoria y se pierden al reiniciar.
# Cuando haya que conservarlas entre despliegues, esto pasa a Postgres, que es
# la misma base que va a necesitar el RAG de la fase 3.
conversaciones: dict[str, Yachaq] = {}


class Pregunta(BaseModel):
    mensaje: str
    conversacion: str | None = None


@app.get("/salud")
def salud():
    """Sirve para saber si el contenedor está vivo y con qué está hablando."""
    agente = next(iter(conversaciones.values()), None)
    return {
        "estado": "vivo",
        "proveedores": list(PROVEEDORES),
        "modelo": agente.modelo if agente else None,
        "herramientas": [e["function"]["name"] for e in herramientas.esquemas()],
        "conversaciones_abiertas": len(conversaciones),
    }


@app.post("/preguntar")
def preguntar(pregunta: Pregunta):
    """Una pregunta en lenguaje natural. Devuelve la respuesta y, a la vista,
    qué herramientas usó el agente para llegar a ella."""
    clave = pregunta.conversacion or uuid.uuid4().hex[:12]
    if clave not in conversaciones:
        conversaciones[clave] = Yachaq()

    salida = conversaciones[clave].responder(pregunta.mensaje)
    return {"conversacion": clave, **salida}


@app.post("/identificar")
async def identificar(foto: UploadFile = File(...), conversacion: str | None = None):
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
        if clave not in conversaciones:
            conversaciones[clave] = Yachaq()
        salida = conversaciones[clave].responder(
            f"Identifica la foto que está en {ruta} y cuéntame qué es y dónde vive.")
        return {"conversacion": clave, **salida}
    except Exception as err:
        return JSONResponse(status_code=500, content={"error": f"{type(err).__name__}: {err}"})
    finally:
        Path(ruta).unlink(missing_ok=True)


@app.delete("/conversacion/{clave}")
def olvidar(clave: str):
    """Cierra una conversación y libera su historia."""
    return {"olvidada": conversaciones.pop(clave, None) is not None}
