"""Lo que el agente sabe hacer de verdad.

Cada función de aquí es una herramienta que el modelo puede decidir usar. No son
adornos: `identificar` carga el modelo de Riksi y clasifica de verdad, y las
otras dos consultan GBIF en vivo.

El esquema que ve el modelo se genera de la firma y del docstring de cada
función. Escribir el esquema aparte sería una segunda fuente de verdad que se
desincroniza en cuanto alguien cambie un argumento.
"""

import inspect
import json
import os
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

GBIF = "https://api.gbif.org/v1"
AGENTE = "yachaq/0.1 (https://github.com/DiegoFernandoLojanTenesaca)"

# El modelo vive en el repositorio de Riksi. Se referencia, no se copia: son
# 3,8 MB que ya están versionados en otro sitio y que cambian cuando se
# reentrena allí.
MODELO = Path(os.environ.get("RIKSI_MODELO", r"D:\CLAUDE PROYECTOS\riksi\docs\modelo"))


@lru_cache(maxsize=1)
def _riksi():
    """El modelo, sus clases y su umbral. Se carga una vez y se queda."""
    sesion = ort.InferenceSession(str(MODELO / "riksi-int8.onnx"),
                                  providers=["CPUExecutionProvider"])
    leer = lambda n: json.loads((MODELO / n).read_text(encoding="utf-8"))
    umbral = leer("umbral.json") if (MODELO / "umbral.json").exists() else {"umbral": 0.4}
    comunes = leer("comunes.json") if (MODELO / "comunes.json").exists() else {}
    return sesion, leer("clases.json"), leer("preprocesado.json"), comunes, umbral


def _preparar(ruta, pre):
    """El mismo preprocesado que usó el entrenamiento. Si difiere, el modelo no
    falla: acierta menos, que es peor porque no se nota."""
    im = Image.open(ruta).convert("RGB")
    escala = pre["resize"] / min(im.size)
    im = im.resize((round(im.width * escala), round(im.height * escala)), Image.BILINEAR)
    tam = pre["tam"]
    izq, arriba = (im.width - tam) // 2, (im.height - tam) // 2
    im = im.crop((izq, arriba, izq + tam, arriba + tam))
    x = np.asarray(im, dtype=np.float32) / 255.0
    x = (x - np.array(pre["media"], dtype=np.float32)) / np.array(pre["desv"], dtype=np.float32)
    return x.transpose(2, 0, 1)[None]


def _pedir(ruta, **params):
    pares = [(k, v) for k, vs in params.items() for v in (vs if isinstance(vs, list) else [vs])]
    url = f"{GBIF}/{ruta}?{urllib.parse.urlencode(pares)}"
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ── las herramientas ────────────────────────────────────────────────────────

def identificar(ruta_de_la_foto: str) -> dict:
    """Identifica la especie de una foto de un animal o una planta del Ecuador.

    Usa un modelo entrenado con fotos de campo que conoce 100 especies. Devuelve
    las tres candidatas más probables con su confianza. Si la mejor no llega al
    umbral, `seguro` es falso y la respuesta debe darse con reservas.

    Args:
        ruta_de_la_foto: ruta del archivo de imagen en el disco.
    """
    sesion, clases, pre, comunes, umbral = _riksi()
    salida = sesion.run(None, {sesion.get_inputs()[0].name: _preparar(ruta_de_la_foto, pre)})[0][0]
    e = np.exp(salida - salida.max())
    probs = e / e.sum()

    top = np.argsort(-probs)[:3]
    return {
        "seguro": bool(probs[top[0]] >= umbral["umbral"]),
        "umbral": umbral["umbral"],
        "candidatas": [
            {
                "especie": clases[i].replace("_", " "),
                "comun": comunes.get(clases[i], ""),
                "confianza": round(float(probs[i]), 3),
            }
            for i in top
        ],
    }


def buscar_especie(nombre: str) -> dict:
    """Busca una especie en GBIF por su nombre común o científico.

    Devuelve el nombre científico aceptado, el reino y la clase, más cuántas
    observaciones con foto hay registradas en Ecuador.

    Args:
        nombre: nombre científico o común, en cualquier idioma.
    """
    m = _pedir("species/match", name=nombre)
    if not m.get("usageKey"):
        return {"encontrada": False, "buscado": nombre}

    ocurrencias = _pedir("occurrence/search", country="EC", speciesKey=m["usageKey"],
                         mediaType="StillImage", limit=0)
    return {
        "encontrada": True,
        "especie": m.get("canonicalName"),
        "grupo": m.get("class") or m.get("phylum"),
        "reino": m.get("kingdom"),
        "coincidencia": m.get("matchType"),
        "observaciones_con_foto_en_ecuador": ocurrencias.get("count", 0),
    }


def donde_se_ha_visto(especie: str, cuantas: int = 20) -> dict:
    """Dice en qué lugares del Ecuador se ha registrado una especie.

    Consulta las observaciones reales de GBIF y agrupa por provincia o
    localidad, de la más frecuente a la menos.

    Args:
        especie: nombre científico de la especie.
        cuantas: cuántos registros consultar. Entre 10 y 300.
    """
    m = _pedir("species/match", name=especie)
    if not m.get("usageKey"):
        return {"encontrada": False, "buscado": especie}

    d = _pedir("occurrence/search", country="EC", speciesKey=m["usageKey"],
               limit=max(10, min(cuantas, 300)))
    sitios, alturas = {}, []
    for oc in d.get("results", []):
        lugar = oc.get("stateProvince") or oc.get("locality") or "sin localidad"
        sitios[lugar] = sitios.get(lugar, 0) + 1
        if oc.get("elevation") is not None:
            alturas.append(oc["elevation"])

    return {
        "encontrada": True,
        "especie": m.get("canonicalName"),
        "registros_revisados": len(d.get("results", [])),
        "total_en_ecuador": d.get("count", 0),
        "lugares": sorted(sitios.items(), key=lambda kv: -kv[1])[:10],
        # Un `None` invita al modelo a rellenar el hueco de memoria y presentarlo
        # con el mismo tono que los datos consultados. Decirlo con palabras
        # cierra esa puerta.
        "altura_metros": (
            {"minima": min(alturas), "maxima": max(alturas), "registros": len(alturas)}
            if alturas else
            "GBIF no trae la altura en estos registros. No hay dato: no lo inventes."
        ),
    }


# ── el esquema que ve el modelo ─────────────────────────────────────────────

TIPOS = {str: "string", int: "integer", float: "number", bool: "boolean"}
CATALOGO = {f.__name__: f for f in (identificar, buscar_especie, donde_se_ha_visto)}


def esquemas():
    """Traduce las firmas a la forma que espera la API, sin escribirlas dos veces."""
    salida = []
    for nombre, funcion in CATALOGO.items():
        firma = inspect.signature(funcion)
        propiedades, obligatorios = {}, []
        for arg, param in firma.parameters.items():
            propiedades[arg] = {"type": TIPOS.get(param.annotation, "string"),
                                "description": arg.replace("_", " ")}
            if param.default is inspect.Parameter.empty:
                obligatorios.append(arg)
        salida.append({
            "type": "function",
            "function": {
                "name": nombre,
                "description": inspect.getdoc(funcion),
                "parameters": {"type": "object", "properties": propiedades,
                               "required": obligatorios},
            },
        })
    return salida


def ejecutar(nombre, argumentos):
    """Corre una herramienta. Un fallo se devuelve como texto, no se lanza: el
    modelo puede leerlo y corregir, y una excepción mataría la conversación."""
    if nombre not in CATALOGO:
        return {"error": f"no existe la herramienta {nombre}"}
    try:
        return CATALOGO[nombre](**argumentos)
    except Exception as err:
        return {"error": f"{type(err).__name__}: {err}"}


def prueba():
    """Comprobación de lo único con lógica propia: el esquema y GBIF."""
    e = {x["function"]["name"]: x for x in esquemas()}
    assert set(e) == set(CATALOGO), e.keys()
    assert e["donde_se_ha_visto"]["function"]["parameters"]["required"] == ["especie"], \
        "cuantas tiene valor por defecto, no debería ser obligatorio"
    assert "identifica" in e["identificar"]["function"]["description"].lower()

    r = buscar_especie("Amblyrhynchus cristatus")
    assert r["encontrada"] and r["especie"] == "Amblyrhynchus cristatus", r
    assert r["observaciones_con_foto_en_ecuador"] > 0, r

    assert ejecutar("no_existe", {})["error"].startswith("no existe")
    assert "error" in ejecutar("buscar_especie", {"mal": 1}), "un argumento inválido debe volver como error"
    print(f"ok · {len(e)} herramientas · GBIF responde · "
          f"{r['observaciones_con_foto_en_ecuador']:,} fotos de la iguana marina")


if __name__ == "__main__":
    prueba()
