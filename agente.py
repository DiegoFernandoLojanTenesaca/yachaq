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
        # Todos los modelos de Groq admiten herramientas; este además puede
        # pedir varias a la vez, que es justo lo que hace falta aquí.
        "modelo": "llama-3.3-70b-versatile",
        "clave_en": "GROQ_API_KEY",
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1",
        "modelo": "gpt-oss-120b",
        "clave_en": "CEREBRAS_API_KEY",
    },
}

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

Responde en español, breve y sin rodeos. Si una herramienta falla, cuéntalo en
una línea y sigue con lo que sí puedas responder."""


def clave(proveedor):
    variable = PROVEEDORES[proveedor]["clave_en"]
    if os.environ.get(variable):
        return os.environ[variable]
    fichero = Path(__file__).parent / "clave.txt"
    if fichero.exists():
        return fichero.read_text(encoding="utf-8").strip()
    sys.exit(f"Falta la clave. Ponla en {variable} o en clave.txt\n"
             f"Se saca gratis en console.groq.com/keys o cloud.cerebras.ai")


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
            respuesta = self.cliente.chat.completions.create(
                model=self.modelo,
                messages=self.historia,
                tools=herramientas.esquemas(),
                temperature=0.3,
            ).choices[0].message

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


def main():
    """Conversación por consola, para probar sin levantar el servidor."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
