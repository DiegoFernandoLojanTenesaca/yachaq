"""El bucle del agente: preguntar, usar herramientas, volver a preguntar.

El proveedor es intercambiable. Groq y Cerebras hablan el mismo dialecto que
OpenAI, así que el mismo código sirve para los dos y para cambiar de uno a otro
cuando uno se cae o se acaba la cuota gratuita. Se elige con dos variables de
entorno y nada más.

La clave NO va en el código ni en el repositorio: se lee de PROVEEDOR_CLAVE o
del fichero clave.txt, que está en el .gitignore.
"""

import json
import os
import sys
from pathlib import Path

from openai import OpenAI

import herramientas

# Groq y Cerebras regalan cuota. Ambos exponen la API con la forma de OpenAI,
# solo cambia la dirección y el nombre del modelo.
PROVEEDORES = {
    "groq": {
        "url": "https://api.groq.com/openai/v1",
        "modelo": "openai/gpt-oss-120b",
        "clave_en": "GROQ_API_KEY",
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1",
        "modelo": "gpt-oss-120b",
        "clave_en": "CEREBRAS_API_KEY",
    },
}

# El catálogo publicado no es el catálogo de tu cuenta: la documentación de Groq
# anuncia llama-3.3-70b-versatile y esta clave responde 404 al pedirlo. Antes de
# fijar un modelo, pregúntale a la API cuáles tiene de verdad:
#
#     python agente.py --modelos

SISTEMA = """Eres Yachaq, un asistente de naturaleza del Ecuador. «Yachaq», en
kichwa, es el que sabe.

Tienes herramientas de verdad y debes usarlas en lugar de responder de memoria:
- Si te dan la ruta de una foto, identifícala con la herramienta. Nunca adivines
  la especie mirando el nombre del archivo.
- Si te preguntan por una especie, búscala antes de afirmar nada sobre ella.
- Si te preguntan dónde vive o dónde verla, consulta los registros reales.

Cuando el modelo de identificación diga que no está seguro, dilo tú también: en
el campo, una identificación equivocada dada con seguridad hace más daño que no
dar ninguna. Da el nombre común primero y el científico después, en cursiva
cuando el medio lo permita.

NO RELLENES LOS HUECOS. Si una herramienta no trae un dato -la altura, el
hábitat, la época de reproducción-, di que no lo tienes y para ahí. No lo
completes con lo que recuerdes: quien lee no puede distinguir un dato consultado
de uno recordado, y los dos van en el mismo párrafo con el mismo tono. Cuando
aportes contexto propio que no salió de una herramienta, dilo en la frase.

Ecuador se divide en PROVINCIAS, no en departamentos ni en estados.

Responde en español, breve y sin rodeos. Si una herramienta falla, cuéntalo en
una línea y sigue con lo que sí puedas responder."""


def clave(proveedor):
    """La clave del proveedor, del entorno o del .env.

    Un fichero por proveedor no escalaba en cuanto hubo dos. El .env se parsea a
    mano (son cuatro líneas) en vez de añadir python-dotenv como dependencia.
    Está en el .gitignore: una clave publicada la revocan, con razón.
    """
    variable = PROVEEDORES[proveedor]["clave_en"]
    if os.environ.get(variable):
        return os.environ[variable]

    entorno = Path(__file__).parent / ".env"
    if entorno.exists():
        for linea in entorno.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea.startswith("#") or "=" not in linea:
                continue
            nombre, _, valor = linea.partition("=")
            if nombre.strip() == variable:
                return valor.strip().strip('"\'')

    sys.exit(f"Falta la clave de {proveedor}. Ponla en {variable} o en .env\n"
             f"Se sacan gratis en console.groq.com/keys y cloud.cerebras.ai")


class Yachaq:
    """Mantiene la conversación y el bucle de herramientas."""

    def __init__(self, proveedor=None, modelo=None):
        self.proveedor = proveedor or os.environ.get("PROVEEDOR", "groq")
        if self.proveedor not in PROVEEDORES:
            sys.exit(f"Proveedor desconocido: {self.proveedor}. Hay {list(PROVEEDORES)}")
        cfg = PROVEEDORES[self.proveedor]
        self.modelo = modelo or os.environ.get("MODELO") or cfg["modelo"]
        self.cliente = OpenAI(base_url=cfg["url"], api_key=clave(self.proveedor))
        self.historia = [{"role": "system", "content": SISTEMA}]

    def responder(self, mensaje, vueltas_maximas=6):
        """Una pregunta, y las llamadas a herramientas que hagan falta.

        El tope de vueltas no es decorativo: un modelo puede quedarse pidiendo
        la misma herramienta en bucle, y sin límite eso es una factura o una
        cuota agotada sin que nadie se entere.
        """
        self.historia.append({"role": "user", "content": mensaje})
        usadas = []

        for _ in range(vueltas_maximas):
            try:
                respuesta = self.cliente.chat.completions.create(
                    model=self.modelo,
                    messages=self.historia,
                    tools=herramientas.esquemas(),
                    temperature=0.3,
                ).choices[0].message
            except Exception as err:
                # Los proveedores gratuitos se acaban, cambian el catálogo y
                # cortan por cuota. Medido: Cerebras responde 402 con esta clave
                # y Groq da 404 si pides un modelo que anuncia su documentación
                # pero tu cuenta no sirve. Nada de eso debe salir como traza.
                return {"respuesta": self._explicar(err), "herramientas": usadas,
                        "error": type(err).__name__}

            # El mensaje del modelo vuelve a la historia tal cual, con sus
            # llamadas: si se pierden, la API rechaza los resultados que vienen
            # después por no tener a qué referirse.
            self.historia.append(respuesta.model_dump(exclude_none=True))

            if not respuesta.tool_calls:
                return {"respuesta": respuesta.content, "herramientas": usadas}

            for llamada in respuesta.tool_calls:
                argumentos = json.loads(llamada.function.arguments or "{}")
                resultado = herramientas.ejecutar(llamada.function.name, argumentos)
                usadas.append({"herramienta": llamada.function.name,
                               "argumentos": argumentos, "resultado": resultado})
                self.historia.append({
                    "role": "tool",
                    "tool_call_id": llamada.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                })

        return {"respuesta": "Me quedé dando vueltas con las herramientas y paré "
                             "para no seguir gastando. Prueba a preguntarlo más concreto.",
                "herramientas": usadas}

    def _explicar(self, err):
        """Traduce el fallo del proveedor a algo accionable."""
        codigo = getattr(err, "status_code", None)
        otro = next(p for p in PROVEEDORES if p != self.proveedor)
        if codigo == 402:
            return (f"{self.proveedor} pide pago: se acabó la cuota gratuita. "
                    f"Prueba con PROVEEDOR={otro}.")
        if codigo == 404:
            return (f"{self.proveedor} no sirve el modelo «{self.modelo}» con esta "
                    f"clave. Mira cuáles tienes con: python agente.py --modelos")
        if codigo == 401:
            return f"La clave de {self.proveedor} no vale. Revísala en el .env."
        if codigo == 429:
            return f"{self.proveedor} está limitando por cuota. Espera un poco o usa PROVEEDOR={otro}."
        return f"{self.proveedor} falló: {type(err).__name__}. {err}"


def listar_modelos():
    """Qué modelos tiene de verdad cada clave. La documentación anuncia unos y
    la cuenta sirve otros; esto zanja la discusión en dos segundos."""
    for nombre, cfg in PROVEEDORES.items():
        try:
            cliente = OpenAI(base_url=cfg["url"], api_key=clave(nombre))
            ids = sorted(m.id for m in cliente.models.list().data)
            print(f"\n{nombre} ({len(ids)}):")
            for i in ids:
                print(f"  {'* ' if i == cfg['modelo'] else '  '}{i}")
        except SystemExit:
            print(f"\n{nombre}: sin clave")
        except Exception as err:
            print(f"\n{nombre}: {type(err).__name__} · {err}")


def main():
    """Conversación por consola, para probar sin levantar el servidor."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--modelos" in sys.argv:
        return listar_modelos()

    agente = Yachaq()
    print(f"Yachaq · {agente.proveedor} · {agente.modelo}")
    print("Escribe una pregunta, o Ctrl+C para salir.\n")
    while True:
        try:
            pregunta = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not pregunta:
            continue
        salida = agente.responder(pregunta)
        for uso in salida["herramientas"]:
            print(f"   [{uso['herramienta']}({uso['argumentos']})]")
        print(f"\n{salida['respuesta']}\n")


if __name__ == "__main__":
    main()
