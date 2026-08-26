"""El índice vectorial: buscar por significado y no por palabra exacta.

**Aquí no hay base de datos, y es a propósito.** Son unos mil fragmentos de 384
dimensiones: un millón y medio de números, once megas en memoria. Compararlos
todos con una multiplicación de matrices tarda menos de un milisegundo, o sea
menos que el viaje de ida y vuelta a un Postgres que estuviera en la misma
máquina. Montar pgvector para esto sería pagar un contenedor, un esquema y una
conexión para ir más lento.

pgvector empieza a ganar cuando los vectores no caben en memoria, cuando varios
procesos escriben a la vez, o cuando hay que filtrar por metadatos antes de
buscar. Nada de eso pasa todavía. Cuando la memoria de la fase 4 traiga
usuarios y escrituras concurrentes, esa será la razón de meterlo, y entonces
solo cambia este fichero.

El modelo de embeddings corre sobre ONNX, el mismo motor que ya usa Riksi para
clasificar: 220 MB y ninguna dependencia nueva de peso. Traerse PyTorch entero
para vectorizar mil párrafos serían dos gigas y medio.

    python fichas.py --indexar
"""

import json
from functools import lru_cache
from pathlib import Path

import numpy as np

AQUI = Path(__file__).parent
FICHAS = AQUI / "fichas.jsonl"
VECTORES = AQUI / "vectores.npy"
MODELO_EMBEDDINGS = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Por debajo de este parecido no se devuelve nada. Sale medido, no a ojo:
# `python indice.py --calibrar` compara lo que puntúan las preguntas con
# respuesta contra lo que puntúan las que no la tienen.
MINIMO = 0.44


@lru_cache(maxsize=1)
def _codificador():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODELO_EMBEDDINGS)


def vectorizar(textos):
    """Textos a vectores, normalizados.

    Se normalizan aquí para que buscar sea un producto escalar y nada más: con
    vectores de norma 1, el coseno y el producto son la misma operación.
    """
    v = np.array(list(_codificador().embed(textos)), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def construir():
    """Calcula los vectores de todos los fragmentos y los deja en disco."""
    if not FICHAS.exists():
        raise SystemExit(f"No hay {FICHAS.name}. Córrelo antes: python fichas.py")

    fragmentos = [json.loads(l) for l in FICHAS.read_text(encoding="utf-8").splitlines() if l]
    print(f"{len(fragmentos)} fragmentos · modelo {MODELO_EMBEDDINGS}")
    print("la primera vez se descarga el modelo (220 MB)…")

    vectores = vectorizar([f["texto"] for f in fragmentos])
    np.save(VECTORES, vectores)
    print(f"{vectores.shape[0]} vectores de {vectores.shape[1]} dimensiones en "
          f"{VECTORES.name} ({VECTORES.stat().st_size / 1024**2:.1f} MB)")
    return vectores


@lru_cache(maxsize=1)
def _cargar():
    if not (FICHAS.exists() and VECTORES.exists()):
        return None, None
    fragmentos = [json.loads(l) for l in FICHAS.read_text(encoding="utf-8").splitlines() if l]
    vectores = np.load(VECTORES)
    if len(fragmentos) != len(vectores):
        raise SystemExit("Las fichas y los vectores no cuadran: vuelve a indexar "
                         "con python fichas.py --indexar")
    return fragmentos, vectores


def buscar(pregunta, cuantos=4, minimo=MINIMO):
    """Los fragmentos más parecidos a la pregunta.

    El corte por parecido no es cosmético: sin él, una pregunta que no tiene
    respuesta en las fichas devuelve igualmente los cuatro fragmentos menos
    malos, y el modelo los usa como si vinieran a cuento. Es preferible
    devolver nada y que diga que no lo sabe.
    """
    fragmentos, vectores = _cargar()
    if fragmentos is None:
        return []

    consulta = vectorizar([pregunta])[0]
    parecidos = vectores @ consulta            # vectores normalizados: coseno
    mejores = np.argsort(-parecidos)[:cuantos]
    return [
        {**fragmentos[i], "parecido": round(float(parecidos[i]), 3)}
        for i in mejores if parecidos[i] >= minimo
    ]


def calibrar():
    """A partir de qué parecido merece la pena responder.

    El corte separa dos poblaciones: preguntas cuya respuesta está en las fichas
    y preguntas que no tienen nada que ver. Puesto a ojo en 0,35, una pregunta
    sobre el volcán más alto de Marte devolvía párrafos de un colibrí con 0,40 y
    el modelo los habría usado como si vinieran a cuento.

    Se elige el punto medio entre lo peor que puntúa una pregunta con respuesta
    y lo mejor que puntúa una sin ella.
    """
    con_respuesta = [
        "¿por qué tiene los pies azules?",
        "¿qué come el hoatzin?",
        "¿dónde anida la iguana marina?",
        "¿cómo es el cortejo del piquero?",
        "¿qué tamaño alcanza la tortuga de Galápagos?",
        "¿de qué color es el colibrí cobrizo?",
        "¿cuánto vive una iguana terrestre?",
        "¿qué hace la fragata con su garganta roja?",
    ]
    sin_respuesta = [
        "¿cómo se llama el volcán más alto de Marte?",
        "¿cuál es la capital de Mongolia?",
        "¿cómo se arregla un motor diésel?",
        "¿qué es la inflación estructural?",
        "¿cuándo se estrenó Blade Runner?",
        "receta de arroz con leche",
    ]

    def mejor(p):
        fragmentos, vectores = _cargar()
        return float((vectores @ vectorizar([p])[0]).max())

    buenas = sorted(mejor(p) for p in con_respuesta)
    malas = sorted((mejor(p) for p in sin_respuesta), reverse=True)
    corte = round((buenas[0] + malas[0]) / 2, 2)

    print(f"{'con respuesta en las fichas':<32}{'sin respuesta':>20}")
    for b, m in zip(buenas, malas + [None] * len(buenas)):
        print(f"  {b:.3f}{'':<26}{m:.3f}" if m is not None else f"  {b:.3f}")
    print(f"\npeor de las buenas {buenas[0]:.3f} · mejor de las malas {malas[0]:.3f}")
    print(f"corte recomendado: {corte}   (ahora en {MINIMO})")
    if buenas[0] <= malas[0]:
        print("SE SOLAPAN: ningún corte las separa. Hace falta más contexto por "
              "fragmento o un modelo de embeddings mejor.")
    return corte


def prueba():
    """Que el índice encuentre por significado y no por coincidencia de letras."""
    fragmentos, vectores = _cargar()
    assert fragmentos, "no hay índice: corre python fichas.py y luego --indexar"
    assert vectores.shape[1] == 384, vectores.shape
    assert np.allclose(np.linalg.norm(vectores, axis=1), 1, atol=1e-3), "sin normalizar"

    # La pregunta no comparte casi ninguna palabra con la respuesta esperada:
    # si esto funciona, está buscando por significado.
    r = buscar("¿por qué tiene los pies de ese color?")
    assert r, "no encontró nada para una pregunta con respuesta en las fichas"
    print(f"ok · {len(fragmentos)} fragmentos · {vectores.shape[1]}d · "
          f"la mejor coincidencia es {r[0]['especie']} ({r[0]['parecido']})")


if __name__ == "__main__":
    import sys
    calibrar() if "--calibrar" in sys.argv else prueba()
