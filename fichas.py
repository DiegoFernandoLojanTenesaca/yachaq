"""Las fichas: de dónde sale el texto que el agente puede citar.

Un RAG sin documentos es un adorno, así que lo primero fue mirar qué texto
existe de verdad para estas especies:

- **Wikipedia en español** tiene artículos completos, de 2.700 a 15.000
  caracteres. Es el cuerpo de la ficha.
- **GBIF** tiene descripciones, pero telegráficas: para el piquero patiazul
  devuelve «Bandera», «Carnívoro», «LC», «Muy especialista». De veinte
  entradas, una sola pasaba de cien caracteres. Se aprovechan las que dicen
  algo y se tiran las demás.

    python fichas.py            # baja y trocea, deja fichas.jsonl
    python fichas.py --indexar  # calcula los vectores
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from herramientas import MODELO, _pedir, _resolver

AQUI = Path(__file__).parent
FICHAS = AQUI / "fichas.jsonl"
WIKI = "https://es.wikipedia.org/w/api.php"
AGENTE = "yachaq/0.1 (https://github.com/DiegoFernandoLojanTenesaca)"

# Un fragmento corto no da contexto y uno largo diluye la parte que importa al
# buscar por similitud. Se corta por párrafos y se acumula hasta este tamaño.
CARACTERES = 900
MINIMO_GBIF = 80          # por debajo de esto, GBIF devuelve etiquetas, no prosa


def wikipedia(titulo):
    """El artículo en español, en texto plano. Vacío si no existe."""
    url = WIKI + "?" + urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "explaintext": 1,
        "redirects": 1, "format": "json", "titles": titulo,
    })
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    pagina = next(iter(d["query"]["pages"].values()))
    return pagina.get("extract") or ""


def trocear(texto, fuente, especie, comun):
    """Parte el texto en fragmentos que quepan enteros en una respuesta.

    Se respetan los párrafos: cortar a ciegas cada N caracteres parte frases por
    la mitad y el fragmento recuperado empieza a media palabra.
    """
    trozos, actual = [], ""
    for parrafo in (p.strip() for p in texto.split("\n") if p.strip()):
        if len(actual) + len(parrafo) > CARACTERES and actual:
            trozos.append(actual)
            actual = parrafo
        else:
            actual = f"{actual}\n{parrafo}" if actual else parrafo
    if actual:
        trozos.append(actual)

    # Cada fragmento lleva de quién habla: al recuperarlo suelto, «mide 80 cm»
    # no dice de qué animal, y el modelo lo atribuiría a lo que estuviera
    # hablando en ese momento.
    return [
        {"especie": especie, "comun": comun, "fuente": fuente,
         "texto": f"{comun or especie} ({especie}). {t}" if n == 0 else
                  f"[{comun or especie} · {especie}] {t}"}
        for n, t in enumerate(trozos)
    ]


def descargar():
    clases = json.loads((MODELO / "clases.json").read_text(encoding="utf-8"))
    comunes = json.loads((MODELO / "comunes.json").read_text(encoding="utf-8"))

    fichas, sin_wiki = [], []
    for n, clase in enumerate(clases, 1):
        especie = clase.replace("_", " ")
        comun = comunes.get(clase, "")

        texto = wikipedia(especie)
        if texto:
            fichas += trocear(texto, "wikipedia-es", especie, comun)
        else:
            sin_wiki.append(especie)

        # GBIF, solo lo que sea prosa. El resto son etiquetas de una palabra
        # que al buscar por similitud solo hacen ruido.
        try:
            clave, _, _, _ = _resolver(especie)
            if clave:
                d = _pedir(f"species/{clave}/descriptions", limit=30)
                util = [r["description"] for r in d.get("results", [])
                        if r.get("description") and len(r["description"]) >= MINIMO_GBIF]
                if util:
                    fichas += trocear("\n".join(util), "gbif", especie, comun)
        except Exception as err:
            print(f"    gbif falló en {especie}: {err}", file=sys.stderr)

        print(f"[{n:>3}/{len(clases)}] {especie:<34} {len(texto):>6} car de wikipedia")
        time.sleep(0.1)          # no atropellar a Wikipedia

    with open(FICHAS, "w", encoding="utf-8") as fh:
        for f in fichas:
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")

    print(f"\n{len(fichas)} fragmentos de {len(clases) - len(sin_wiki)} especies "
          f"en {FICHAS.name}")
    if sin_wiki:
        print(f"{len(sin_wiki)} sin artículo en Wikipedia: {', '.join(sin_wiki[:5])}"
              + ("…" if len(sin_wiki) > 5 else ""))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--indexar" in sys.argv:
        import indice
        return indice.construir()
    descargar()


if __name__ == "__main__":
    main()
