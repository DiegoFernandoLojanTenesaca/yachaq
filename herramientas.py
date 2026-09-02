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

# El RAG cuesta 330 MB de RAM: sin él el agente ocupa 130 y con él 457. En un
# servidor gratuito de 256 MB eso es la diferencia entre arrancar y morir al
# primer uso, así que se puede apagar sin tocar el código.
#
# No se apaga en silencio: `consultar_fichas` desaparece del catálogo, de modo
# que el modelo no la ve y no promete lo que no puede cumplir. Un agente que
# ofrece una herramienta rota es peor que uno que no la ofrece.
SIN_RAG = os.environ.get("YACHAQ_SIN_RAG", "").lower() in ("1", "true", "si", "sí")


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


def _resolver(nombre):
    """El nombre que da una persona, convertido en una especie de GBIF.

    `species/match` está hecho para nombres científicos y con los comunes miente
    de dos formas que no parecen mentiras:

    - «piquero patiazul» no lo encuentra, pero devuelve **confianza 100**;
    - «colibri cobrizo» devuelve el GÉNERO `Colibri` con confianza 92, y un
      género colado como especie se propaga a todas las respuestas siguientes.

    Por eso solo se acepta una coincidencia exacta de nombre científico. Si no
    la hay, se busca entre los nombres vernáculos, que es el índice que sí
    contiene «piquero patiazul» y devuelve *Sula nebouxii*.
    """
    m = _pedir("species/match", name=nombre)
    if m.get("matchType") == "EXACT" and m.get("rank") in ("SPECIES", "SUBSPECIES"):
        return m.get("usageKey"), m.get("canonicalName"), m, "nombre científico"

    d = _pedir("species/search", q=nombre, qField="VERNACULAR", rank="SPECIES", limit=5)
    for r in d.get("results", []):
        if r.get("nubKey") or r.get("key"):
            clave = r.get("nubKey") or r["key"]
            return clave, r.get("canonicalName"), r, "nombre común"

    return None, None, {}, None


def buscar_especie(nombre: str) -> dict:
    """Busca una especie en GBIF por su nombre común o científico.

    Devuelve el nombre científico aceptado, el reino y la clase, más cuántas
    observaciones con foto hay registradas en Ecuador. Dice también por qué vía
    la encontró, para que se sepa si el nombre era exacto o una aproximación.

    Args:
        nombre: nombre científico o común, en cualquier idioma.
    """
    clave, cientifico, ficha, via = _resolver(nombre)
    if not clave:
        return {"encontrada": False, "buscado": nombre,
                "nota": "GBIF no reconoce ese nombre. Pide el nombre científico "
                        "en vez de responder sobre una especie parecida."}

    ocurrencias = _pedir("occurrence/search", country="EC", speciesKey=clave,
                         mediaType="StillImage", limit=0)
    return {
        "encontrada": True,
        "especie": cientifico,
        "grupo": ficha.get("class") or ficha.get("phylum"),
        "reino": ficha.get("kingdom"),
        "encontrada_por": via,
        "observaciones_con_foto_en_ecuador": ocurrencias.get("count", 0),
    }


def donde_se_ha_visto(especie: str, cuantas: int = 20, lugar: str = "") -> dict:
    """Dice en qué lugares del Ecuador se ha registrado una especie.

    Consulta las observaciones reales de GBIF y agrupa por provincia o
    localidad, de la más frecuente a la menos.

    Si preguntan por un sitio concreto -un parque, una reserva, un cerro-, pon
    su nombre en `lugar` en vez de deducir en qué provincia cae: tú no sabes
    dónde queda el Cajas, y GBIF sí sabe qué se ha visto allí.

    Args:
        especie: nombre científico de la especie.
        cuantas: cuántos registros consultar. Entre 10 y 300.
        lugar: nombre de un sitio para acotar la búsqueda. Vacío para todo el país.
    """
    clave, cientifico, _, via = _resolver(especie)
    if not clave:
        return {"encontrada": False, "buscado": especie,
                "nota": "GBIF no reconoce ese nombre. No respondas por una especie parecida."}

    filtro = {"q": lugar} if lugar.strip() else {}
    d = _pedir("occurrence/search", country="EC", speciesKey=clave,
               limit=max(10, min(cuantas, 300)), **filtro)
    if lugar.strip() and not d.get("count"):
        return {"encontrada": True, "especie": cientifico, "buscado_en": lugar,
                "total_en_ecuador": 0,
                "nota": f"GBIF no tiene ningún registro de esta especie que "
                        f"mencione «{lugar}». Eso no prueba que no esté: prueba "
                        f"sin acotar el lugar antes de afirmar que no vive ahí."}
    sitios, alturas = {}, []
    for oc in d.get("results", []):
        lugar = oc.get("stateProvince") or oc.get("locality") or "sin localidad"
        sitios[lugar] = sitios.get(lugar, 0) + 1
        if oc.get("elevation") is not None:
            alturas.append(oc["elevation"])

    return {
        "encontrada": True,
        "especie": cientifico,
        "encontrada_por": via,
        "buscado_en": lugar or "todo el Ecuador",
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


def consultar_fichas(pregunta: str) -> dict:
    """Busca en las fichas de las especies para responder sobre su biología.

    Sirve para lo que no está en los registros de observaciones: qué come, cómo
    se reproduce, por qué tiene ese color, cuánto vive, qué amenazas tiene. El
    texto viene de Wikipedia en español y de las descripciones de GBIF.

    Devuelve los párrafos que vienen al caso. Si devuelve `fragmentos` vacío es
    que la respuesta NO está en las fichas: dilo en vez de contestar de memoria.

    Args:
        pregunta: la pregunta tal cual, con sus palabras.
    """
    import indice
    trozos = indice.buscar(pregunta)
    if not trozos:
        return {"fragmentos": [],
                "nota": "Ninguna ficha habla de eso. No hay material: dilo y no "
                        "lo completes de memoria."}
    return {
        "fragmentos": [
            {"especie": t["especie"], "comun": t["comun"], "fuente": t["fuente"],
             "parecido": t["parecido"], "texto": t["texto"]}
            for t in trozos
        ],
    }


def especies_que_conozco() -> dict:
    """Las 100 especies que el identificador reconoce, con su nombre común.

    Llámala ANTES de recomendar qué buscar o de responder a una pregunta
    abierta. Sin ella no sabes de qué puedes hablar con datos y acabas
    consultando especies una por una, o proponiendo alguna que ni siquiera vive
    en el Ecuador.
    """
    _, clases, _, comunes, _ = _riksi()
    return {
        "cuantas": len(clases),
        "especies": [f"{c.replace('_', ' ')}" + (f" ({comunes[c]})" if comunes.get(c) else "")
                     for c in clases],
        "nota": "Fuera de esta lista no puedes identificar ni tienes fichas. "
                "Puedes buscar otra especie en GBIF, pero dilo.",
    }


def recordar(hecho: str, tipo: str = "dato") -> dict:
    """Guarda algo sobre esta persona para conversaciones futuras.

    Úsala cuando cuente algo suyo que seguirá siendo verdad la semana que viene:
    qué grupo le interesa, por dónde sale al campo, con qué equipo, qué nivel
    tiene. NO la uses para lo que se agota en esta conversación -la especie que
    acaba de preguntar, la foto que acaba de subir- ni para datos de las
    especies, que ya están en las fichas.

    Escribe el hecho en tercera persona y entero, porque se leerá suelto y
    dentro de un mes: «sale al campo por el Parque Nacional Cajas», no «el
    Cajas».

    Si te cuentan dos cosas en la misma frase, llama dos veces: un hecho por
    llamada. «Me gustan los colibríes y salgo por el Cajas» son un interés y un
    lugar, y guardarlos juntos hace imposible olvidar uno sin el otro.

    Args:
        hecho: la frase que hay que recordar, en tercera persona.
        tipo: interes, lugar o dato.
    """
    import memoria
    return memoria.guardar(hecho, tipo)


def olvidar(sobre: str) -> dict:
    """Borra un recuerdo cuando la persona pide que lo olvides.

    Busca el recuerdo que más se parezca y lo borra. Devuelve cuál era: dilo en
    la respuesta, para que se pueda avisar si se borró el que no era.

    Args:
        sobre: lo que hay que olvidar, con las palabras de quien lo pide.
    """
    import memoria
    return memoria.borrar(sobre)


# ── el esquema que ve el modelo ─────────────────────────────────────────────

TIPOS = {str: "string", int: "integer", float: "number", bool: "boolean"}
CATALOGO = {f.__name__: f for f in (identificar, buscar_especie, donde_se_ha_visto,
                                    consultar_fichas, especies_que_conozco,
                                    recordar, olvidar)
            if not (SIN_RAG and f is consultar_fichas)}


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

    # Los nombres comunes son la trampa: `species/match` devuelve para
    # «piquero patiazul» un no-match con confianza 100, y para «colibri
    # cobrizo» el GÉNERO Colibri con confianza 92. Un género colado como
    # especie contamina todas las respuestas siguientes.
    c = buscar_especie("piquero patiazul")
    assert c["encontrada"] and c["especie"] == "Sula nebouxii", c
    assert c["encontrada_por"] == "nombre común", c

    g = buscar_especie("colibri cobrizo")
    assert g["especie"] != "Colibri", "se coló el género como si fuera la especie"

    assert not buscar_especie("kdjfhskjdfh")["encontrada"]

    if SIN_RAG:
        assert "consultar_fichas" not in CATALOGO, "el RAG está apagado pero sigue anunciado"
        print(f"ok · {len(e)} herramientas · GBIF responde · RAG apagado por "
              f"YACHAQ_SIN_RAG (ahorra 330 MB)")
        return

    # El RAG tiene que saber callarse: si devolviera «lo menos malo» para
    # cualquier cosa, el modelo lo usaría como si viniera a cuento.
    f = consultar_fichas("¿por qué tiene los pies azules?")
    assert f["fragmentos"] and f["fragmentos"][0]["especie"] == "Sula nebouxii", f
    assert not consultar_fichas("¿cuál es la capital de Mongolia?")["fragmentos"],         "el RAG respondió a algo que no está en las fichas"

    # Acotar por lugar es lo que evita que el agente deduzca la geografía. El
    # Cajas está en Azuay, y una respuesta anterior lo situó en Pichincha
    # cruzando de memoria el nombre del parque con la provincia más frecuente.
    caj = donde_se_ha_visto("Metallura tyrianthina", cuantas=100, lugar="Cajas")
    provincias = [p for p, _ in caj["lugares"]]
    assert "Azuay" in provincias, f"el filtro por lugar no acota: {provincias}"

    cat = especies_que_conozco()
    assert cat["cuantas"] == len(cat["especies"]) == 100, cat["cuantas"]
    con_comun = [e for e in cat["especies"] if "(" in e]
    assert len(con_comun) > 50, f"solo {len(con_comun)}/100 llevan nombre común"

    assert ejecutar("no_existe", {})["error"].startswith("no existe")
    assert "error" in ejecutar("buscar_especie", {"mal": 1}), "un argumento inválido debe volver como error"
    print(f"ok · {len(e)} herramientas · GBIF responde · "
          f"{r['observaciones_con_foto_en_ecuador']:,} fotos de la iguana marina · "
          f"«piquero patiazul» resuelve a {c['especie']}")


if __name__ == "__main__":
    prueba()
