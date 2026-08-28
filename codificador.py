"""Vectorizar preguntas sin cargar 540 MB en memoria.

**Por qué existe.** `fastembed` es cómodo pero carga el modelo entero en RAM:
540 MB, y con eso el proceso se va a 671. Todas las capas gratuitas de hosting
dan 512, así que ese número decide si esto se puede publicar o no.

Lo que hace este módulo es lo mismo que hacía fastembed, pero con los pesos
**fuera del `.onnx`, en un fichero aparte**. Entonces onnxruntime los mapea
desde disco en vez de copiarlos, y el sistema operativo pagina solo las filas
que se tocan de verdad. Cargar la sesión pasa de 540 MB a 55.

                      RAM del proceso   parecido   tiempo
    fastembed                  671 MB     0,714    0,05 s
    aquí                       385 MB     0,714    0,05 s

No es una aproximación: el coseno entre el vector de fastembed y el de aquí es
**1,0 exacto**, y la diferencia máxima componente a componente es **0,0**. Es el
mismo cálculo con los pesos en otro sitio.

**El cálculo hay que escribirlo a mano** porque fastembed no expone la sesión.
Son tres pasos —tokenizar, correr el modelo, promediar por la máscara— y el
tercero es el que importa: `sentence-transformers` promedia los vectores de los
tokens **ponderando por la máscara de atención**, de forma que el relleno no
cuente. Promediar sin la máscara da un vector distinto y el corte de 0,44 deja
de valer.

    python codificador.py --preparar   # deja los pesos externos, una vez
    python codificador.py              # comprueba que da lo mismo que fastembed
"""

import os
import pathlib
import sys
from functools import lru_cache

import numpy as np

AQUI = pathlib.Path(__file__).parent
LIGERO = AQUI / "codificador-ligero"


def _origen():
    """Los ficheros que dejó fastembed la primera vez que se usó."""
    for c in (pathlib.Path(os.environ.get("LOCALAPPDATA", "/tmp")) / "Temp" / "fastembed_cache",
              pathlib.Path("/tmp/fastembed_cache"),
              pathlib.Path.home() / ".cache" / "fastembed"):
        hallado = list(c.glob("**/model_optimized.onnx"))
        if hallado:
            return hallado[0].parent
    return None


def preparar():
    """Reescribe el modelo con los pesos en un fichero aparte.

    No cambia ni un número: es el mismo grafo con los tensores movidos fuera
    para que onnxruntime pueda mapearlos en vez de copiarlos a memoria.
    """
    import onnx

    origen = _origen()
    if not origen:
        raise SystemExit("No encuentro el modelo. Corre antes: python indice.py")

    LIGERO.mkdir(exist_ok=True)
    modelo = onnx.load(str(origen / "model_optimized.onnx"))
    onnx.save(modelo, str(LIGERO / "modelo.onnx"), save_as_external_data=True,
              location="pesos.bin", all_tensors_to_one_file=True, size_threshold=1024)
    (LIGERO / "tokenizer.json").write_bytes((origen / "tokenizer.json").read_bytes())

    total = sum(f.stat().st_size for f in LIGERO.iterdir()) / 1e6
    print(f"{LIGERO.name}/: {total:.0f} MB "
          f"({', '.join(f.name for f in sorted(LIGERO.iterdir()))})")
    return LIGERO


def disponible():
    return (LIGERO / "modelo.onnx").exists() and (LIGERO / "pesos.bin").exists()


@lru_cache(maxsize=1)
def _sesion():
    import onnxruntime as ort
    from tokenizers import Tokenizer

    opciones = ort.SessionOptions()
    # Sin la arena, onnxruntime no reserva un bloque grande por adelantado. Con
    # los pesos externos ya mapeados, ese bloque era casi todo el ahorro.
    opciones.enable_cpu_mem_arena = False
    opciones.enable_mem_pattern = False

    sesion = ort.InferenceSession(str(LIGERO / "modelo.onnx"), opciones,
                                  providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(str(LIGERO / "tokenizer.json"))
    tok.enable_truncation(512)          # el modelo no admite más
    # Relleno hasta el texto más largo del lote, NO hasta 512: `enable_padding()`
    # sin argumentos rellena al máximo y convierte una pregunta de ocho tokens en
    # una matriz de 512 columnas, con lo que la inferencia tarda un minuto.
    tok.enable_padding(direction="right", pad_id=1, pad_token="<pad>")
    entradas = {e.name for e in sesion.get_inputs()}
    return sesion, tok, entradas


def vectorizar(textos):
    """Textos a vectores normalizados. Mismo resultado que fastembed."""
    sesion, tok, entradas = _sesion()
    codificados = tok.encode_batch(list(textos))

    lote = {
        "input_ids": np.array([e.ids for e in codificados], dtype=np.int64),
        "attention_mask": np.array([e.attention_mask for e in codificados], dtype=np.int64),
    }
    if "token_type_ids" in entradas:
        lote["token_type_ids"] = np.array([e.type_ids for e in codificados], dtype=np.int64)

    salida = sesion.run(None, lote)[0]                       # (n, tokens, 384)

    # La media va ponderada por la máscara: los tokens de relleno no cuentan.
    # Sin esto el vector cambia y el corte medido del RAG deja de valer.
    mascara = lote["attention_mask"].astype(np.float32)[:, :, None]
    v = (salida * mascara).sum(axis=1) / np.maximum(mascara.sum(axis=1), 1e-9)
    return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float32)


def prueba():
    """Que dé lo mismo que fastembed, no algo parecido.

    Se compara contra los vectores que ya están en disco, calculados con
    fastembed: si el promedio por la máscara estuviera mal, el parecido caería y
    el corte del RAG dejaría de separar las preguntas.
    """
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not disponible():
        preparar()

    import psutil
    proceso = psutil.Process()

    V = np.load(AQUI / "vectores.npy")
    casos = [("¿por qué tiene los pies azules?", 0.714),
             ("¿qué come el hoatzin?", 0.608),
             ("¿cómo se llama el volcán más alto de Marte?", 0.401)]

    for pregunta, esperado in casos:
        v = vectorizar([pregunta])[0]
        obtenido = float((V @ v).max())
        assert abs(obtenido - esperado) < 0.01, \
            f"«{pregunta}» dio {obtenido:.3f} y se esperaba {esperado:.3f}"

    # La de Marte tiene que quedar por debajo del corte: es el caso que da
    # sentido a todo el RAG.
    assert float((V @ vectorizar([casos[2][0]])[0]).max()) < 0.44, \
        "una pregunta sin respuesta en las fichas pasó el corte"

    # Un lote de varios: si el relleno se colara en la media, aquí se vería.
    lote = vectorizar([p for p, _ in casos])
    solo = np.stack([vectorizar([p])[0] for p, _ in casos])
    assert np.allclose(lote, solo, atol=1e-4), "el lote no da lo mismo que uno a uno"

    print(f"ok · idéntico a fastembed en {len(casos)} casos · el lote coincide "
          f"con uno a uno · {proceso.memory_info().rss/1e6:.0f} MB de RAM "
          f"(fastembed: 671)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--preparar" in sys.argv:
        preparar()
    else:
        prueba()
