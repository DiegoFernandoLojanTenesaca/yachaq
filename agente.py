"""El bucle del agente: preguntar, usar herramientas, volver a preguntar.

**El proveedor es intercambiable, y encima se cambia solo.** Seis servicios
distintos hablan el dialecto de OpenAI, así que el mismo código vale para todos:
si Groq corta por cuota a media conversación, la pregunta sale por Google, y
quien preguntó no se entera más que por una línea al pie de la respuesta.

Eso importa porque estas cuentas son gratuitas y se caen a menudo: de nueve
claves probadas, dos piden pago con saldo 0 y una está retirada. Un agente
atado a un solo proveedor gratuito es un agente que no funciona la mitad de los
días.

La clave NO va en el código ni en el repositorio: se lee del entorno o del
fichero .env, que está en el .gitignore.
"""

import json
import os
import sys
from pathlib import Path

from openai import OpenAI

import herramientas
import memoria

# Todos hablan el dialecto de OpenAI, así que cambia la dirección y el nombre
# del modelo y nada más. El orden es el de la cascada: si el primero no
# responde, se prueba el siguiente.
#
# **Los modelos están elegidos a mano, no por lo que anuncia cada web.** Se
# probaron todos con una llamada a herramienta de verdad, que es lo único que
# necesita este agente: `python agente.py --probar` repite la prueba.
PROVEEDORES = {
    "groq": {
        "url": "https://api.groq.com/openai/v1",
        "modelo": "openai/gpt-oss-120b",
        "clave_en": "GROQ_API_KEY",
    },
    "google": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "modelo": "gemini-2.5-flash",
        "clave_en": "GOOGLE_API_KEY",
    },
    "mistral": {
        # `mistral-large-latest` da timeout con esta cuenta; medium responde.
        "url": "https://api.mistral.ai/v1",
        "modelo": "mistral-medium-latest",
        "clave_en": "MISTRAL_API_KEY",
    },
    "cohere": {
        "url": "https://api.cohere.ai/compatibility/v1",
        "modelo": "command-a-03-2025",
        "clave_en": "COHERE_API_KEY",
    },
    "openrouter": {
        # Los modelos «:free» del catálogo devuelven 404 con esta clave: el
        # sufijo anuncia una cuota que la cuenta no tiene.
        "url": "https://openrouter.ai/api/v1",
        "modelo": "openai/gpt-oss-120b",
        "clave_en": "OPENROUTER_API_KEY",
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1",
        "modelo": "deepseek-ai/deepseek-v4-flash-0731",
        "clave_en": "NVIDIA_API_KEY",
    },
    # Estos dos siguen aquí para que se vea que se probaron y por qué no están
    # en la cascada. Ninguno responde con las claves de este proyecto:
    # Cerebras y SambaNova devuelven 402 con saldo 0. AI21 se quitó del todo:
    # su API respondió 410, retirada.
    "cerebras": {
        "url": "https://api.cerebras.ai/v1",
        "modelo": "gpt-oss-120b",
        "clave_en": "CEREBRAS_API_KEY",
        "de_pago": True,
    },
    "sambanova": {
        "url": "https://api.sambanova.ai/v1",
        "modelo": "Meta-Llama-3.3-70B-Instruct",
        "clave_en": "SAMBANOVA_API_KEY",
        "de_pago": True,
    },
}

# El orden importa: primero el más rápido que funciona, y detrás los que sirven
# de red. Los marcados de pago no entran: probarlos solo añade una espera antes
# de un 402 seguro.
CASCADA = [p for p, c in PROVEEDORES.items() if not c.get("de_pago")]

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
- Si te preguntan por su biología -qué come, cómo cría, por qué es de ese
  color, cuánto vive-, busca en las fichas antes de contestar.
- Si te piden una recomendación o algo abierto, mira primero qué especies
  conoces. No propongas ni consultes una por una a ciegas.
- Si te cuentan algo suyo que seguirá siendo verdad la semana que viene -qué le
  interesa, por dónde sale al campo-, guárdalo con `recordar` y sigue con la
  respuesta sin darle importancia. No anuncies que lo has apuntado a menos que
  te lo pregunten, y no guardes lo que se agota en esta conversación.

Cuando el modelo de identificación diga que no está seguro, dilo tú también: en
el campo, una identificación equivocada dada con seguridad hace más daño que no
dar ninguna. Da el nombre común primero y el científico después, en cursiva
cuando el medio lo permita.

NO RELLENES LOS HUECOS. Si una herramienta no trae un dato -la altura, el
hábitat, la época de reproducción-, di que no lo tienes y para ahí. No lo
completes con lo que recuerdes: quien lee no puede distinguir un dato consultado
de uno recordado, y los dos van en el mismo párrafo con el mismo tono. Cuando
aportes contexto propio que no salió de una herramienta, dilo en la frase.

Ecuador se divide en PROVINCIAS, no en departamentos ni en estados. Y tú no
sabes en qué provincia cae un parque o una reserva: no lo deduzcas. Si te
nombran un sitio, pásale ese nombre a la herramienta y deja que respondan los
registros. Una vez se dijo «Pichincha, donde está el Cajas», y el Cajas está en
Azuay: eso es inventarse un dato con el tono de haberlo consultado.

Responde en español, breve y sin rodeos. Si una herramienta falla, cuéntalo en
una línea y sigue con lo que sí puedas responder."""


def clave(proveedor):
    """La clave del proveedor, del entorno o del .env.

    Devuelve None si no la hay, y no mata el proceso: con una cascada de seis
    proveedores, faltar una clave es lo normal y lo que toca es pasar al
    siguiente.

    Un fichero por proveedor no escalaba en cuanto hubo dos. El .env se parsea a
    mano en vez de añadir python-dotenv como dependencia. Está en el .gitignore:
    una clave publicada la revocan, con razón.
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

    return None


class Yachaq:
    """Mantiene la conversación y el bucle de herramientas."""

    def __init__(self, proveedor=None, modelo=None, usuario="anonimo", conversacion=None):
        pedido = proveedor or os.environ.get("PROVEEDOR")
        if pedido and pedido not in PROVEEDORES:
            sys.exit(f"Proveedor desconocido: {pedido}. Hay {list(PROVEEDORES)}")

        # Si se pide uno a mano va primero, pero los demás siguen detrás como
        # red. Sin clave no entra en la cascada: probarlo es un viaje seguro a
        # un 401.
        orden = ([pedido] + [p for p in CASCADA if p != pedido]) if pedido else list(CASCADA)
        self.cascada = [p for p in orden if clave(p)]
        self.caidos = []
        if not self.cascada:
            sys.exit("No hay ninguna clave. Pon al menos una en .env:\n  "
                     + "\n  ".join(sorted({c["clave_en"] + "=..." for c in PROVEEDORES.values()})))

        self.modelo_pedido = modelo or os.environ.get("MODELO")
        self._usar(self.cascada[0])
        self.usuario = usuario
        self.conversacion = conversacion

        # La historia se recupera si existía, pero el mensaje de sistema se
        # rehace siempre: lleva dentro los recuerdos, y si se restaurara el de
        # entonces, lo aprendido después de guardar la conversación no existiría
        # al reanudarla.
        guardada = memoria.cargar_conversacion(conversacion) if conversacion else None
        self.historia = guardada[1] if guardada else []
        self.historia[:1] = [{"role": "system",
                              "content": SISTEMA + memoria.recordatorio(usuario)}]

    def _usar(self, proveedor):
        cfg = PROVEEDORES[proveedor]
        self.proveedor = proveedor
        self.modelo = self.modelo_pedido or cfg["modelo"]
        self.cliente = OpenAI(base_url=cfg["url"], api_key=clave(proveedor), timeout=90)

    def _siguiente(self, err):
        """Pasa al siguiente proveedor de la cascada. False si no queda ninguno.

        Un modelo pedido a mano no se arrastra al proveedor nuevo: los nombres
        no coinciden entre servicios y llevárselo garantiza un 404 en cadena.
        """
        quedan = self.cascada[self.cascada.index(self.proveedor) + 1:]
        if not quedan:
            return False
        self.caidos.append(f"{self.proveedor} ({self._motivo(err)})")
        self.modelo_pedido = None
        self._usar(quedan[0])
        return True

    @staticmethod
    def _motivo(err):
        return {402: "sin cuota", 404: "modelo no servido", 401: "clave invalida",
                429: "limitado por ritmo", 410: "API retirada",
                }.get(getattr(err, "status_code", None), type(err).__name__)

    def responder(self, mensaje, vueltas_maximas=6):
        """Una pregunta, y las llamadas a herramientas que hagan falta.

        El tope de vueltas no es decorativo: un modelo puede quedarse pidiendo
        la misma herramienta en bucle, y sin límite eso es una factura o una
        cuota agotada sin que nadie se entere.
        """
        # Quién pregunta no es un argumento de las herramientas: si lo fuera, el
        # modelo podría escribir otro nombre y leer la memoria de alguien más.
        memoria.USUARIO.set(self.usuario)
        self.historia.append({"role": "user", "content": mensaje})
        usadas, reintentado, self.caidos = [], False, []

        for _ in range(vueltas_maximas + 1):
            try:
                respuesta = self.cliente.chat.completions.create(
                    model=self.modelo,
                    messages=self.historia,
                    tools=herramientas.esquemas(),
                    temperature=0.3,
                ).choices[0].message
            except Exception as err:
                # `output_parse_failed`: el modelo escribió su razonamiento donde
                # tocaba una llamada a herramienta y el proveedor no pudo
                # parsearlo. Pasa de vez en cuando con los modelos abiertos y se
                # arregla solo al reintentar, así que se reintenta una vez antes
                # de rendirse: es un tropiezo del muestreo, no un fallo de la
                # petición.
                if "output_parse_failed" in str(err) and not reintentado:
                    reintentado = True
                    continue
                # Lo demás es el proveedor cayéndose, y con cuentas gratuitas
                # eso pasa a diario: 402 sin cuota, 404 por un modelo que la web
                # anuncia y la cuenta no sirve, 429 por ritmo. En vez de
                # devolver el error se prueba el siguiente de la cascada con la
                # MISMA historia: lo que ya se consultó sigue ahí y no se repite.
                if self._siguiente(err):
                    reintentado = False
                    continue
                return {"respuesta": self._explicar(err), "herramientas": usadas,
                        "error": type(err).__name__, "proveedores_caidos": self.caidos}

            # El mensaje del modelo vuelve a la historia tal cual, con sus
            # llamadas: si se pierden, la API rechaza los resultados que vienen
            # después por no tener a qué referirse.
            self.historia.append(respuesta.model_dump(exclude_none=True))

            if not respuesta.tool_calls:
                return self._cerrar({"respuesta": respuesta.content,
                                     "herramientas": usadas})

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

        # Se acabaron las vueltas. En vez de devolver una rendición sin
        # contenido, se pide una última respuesta SIN herramientas: el modelo ya
        # tiene en la historia todo lo que consultó, y media respuesta con datos
        # reales vale más que ninguna.
        try:
            ultima = self.cliente.chat.completions.create(
                model=self.modelo,
                messages=self.historia + [{"role": "user", "content":
                    "Se acabaron las consultas. Responde ya con lo que averiguaste, "
                    "y di qué te quedó por mirar."}],
                temperature=0.3,
            ).choices[0].message
            self.historia.append(ultima.model_dump(exclude_none=True))
            return self._cerrar({"respuesta": ultima.content, "herramientas": usadas,
                                 "vueltas_agotadas": True})
        except Exception as err:
            return self._cerrar({"respuesta": self._explicar(err), "herramientas": usadas,
                                 "vueltas_agotadas": True})

    def _cerrar(self, salida):
        """Deja la conversación en disco antes de devolver la respuesta.

        Al terminar el turno y no en cada mensaje: si el proceso se cae a media
        llamada, lo que se pierde es un turno, y la alternativa era guardar una
        historia con una llamada a herramienta sin su resultado, que la API
        rechaza al reanudar.
        """
        if self.conversacion:
            memoria.guardar_conversacion(self.conversacion, self.usuario, self.historia)
        salida["proveedor"] = self.proveedor
        # Quien pregunta tiene derecho a saber que su respuesta la escribio otro
        # modelo porque el primero se cayo: no es lo mismo leerla de uno que de
        # otro, y ocultarlo seria vender continuidad que no hubo.
        if self.caidos:
            salida["proveedores_caidos"] = self.caidos
            salida["respuesta"] = (salida["respuesta"] or "") + (
                f"\n\n_(lo respondio {self.proveedor}; "
                f"{', '.join(self.caidos)} no estaban disponibles)_")
        return salida

    def _explicar(self, err):
        """El fallo cuando ya no queda a quién preguntar.

        Aquí solo se llega con la cascada agotada, así que no vale decir «prueba
        con otro proveedor»: ya se probaron todos.
        """
        caidos = ", ".join(self.caidos) or "ninguno"
        return (f"Se cayeron todos los proveedores. El ultimo, {self.proveedor}: "
                f"{self._motivo(err)}. Antes: {caidos}. Mira que sirve cada clave "
                f"con: python agente.py --probar")


SUMA = [{"type": "function", "function": {
    "name": "sumar", "description": "Suma dos numeros enteros",
    "parameters": {"type": "object",
                   "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                   "required": ["a", "b"]}}}]


def probar_proveedores():
    """Cual de las claves sirve de verdad, y para lo que hace falta.

    No basta con que la clave valga: este agente no sirve de nada con un modelo
    que no llama a herramientas, y eso no sale en ninguna documentacion. Asi que
    la prueba es una llamada a herramienta de verdad, y lo que se mide es si
    vuelve el `tool_call`.

    De aqui salio la tabla de PROVEEDORES: nueve claves probadas, seis que
    responden. Cuando alguna se caiga, esto lo dice en veinte segundos.
    """
    for nombre, cfg in PROVEEDORES.items():
        api = clave(nombre)
        if not api:
            print(f"{nombre:11} sin clave en .env")
            continue
        c = OpenAI(base_url=cfg["url"], api_key=api, timeout=60)
        try:
            r = c.chat.completions.create(
                model=cfg["modelo"], max_tokens=300, tools=SUMA,
                messages=[{"role": "user", "content": "cuanto es 17+25? usa la herramienta"}])
            llamadas = r.choices[0].message.tool_calls
            marca = "OK  " if llamadas else "responde pero NO llama a herramientas"
            print(f"{nombre:11} {marca} {cfg['modelo']}"
                  + (f"  ->  {llamadas[0].function.arguments}" if llamadas else ""))
        except Exception as err:
            print(f"{nombre:11} FALLA  {cfg['modelo']}  ({Yachaq._motivo(err)})")

    print(f"\ncascada en uso: {' -> '.join(CASCADA)}")


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


def prueba():
    """Que la cascada cambie de proveedor sin perder lo ya consultado.

    Se rompe el primero a proposito con un modelo inexistente: es la forma
    honesta de provocar la caida sin esperar a que se agote una cuota de verdad.
    Lo que se comprueba no es que no reviente, sino que la respuesta salga por
    otro proveedor CON las llamadas a herramientas que ya se habian hecho: si se
    perdieran, cada caida costaria repetir todas las consultas.
    """
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    PROVEEDORES["groq"]["modelo"] = "modelo-que-no-existe-000"
    a = Yachaq(usuario="prueba")
    salida = a.responder("¿en qué provincias del Ecuador se ha visto la iguana marina?")

    assert salida.get("proveedores_caidos"), "no cambio de proveedor al fallar el primero"
    assert salida["proveedor"] != "groq", salida["proveedor"]
    assert salida["herramientas"], "perdio las llamadas a herramientas al cambiar"
    assert "Galápagos" in salida["respuesta"], salida["respuesta"][:200]
    print(f"ok · cayo {len(salida['proveedores_caidos'])} y respondio "
          f"{salida['proveedor']} con {len(salida['herramientas'])} consultas intactas")


def main():
    """Conversación por consola, para probar sin levantar el servidor."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--modelos" in sys.argv:
        return listar_modelos()
    if "--probar" in sys.argv:
        return probar_proveedores()
    if "--comprobar" in sys.argv:
        return prueba()

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
