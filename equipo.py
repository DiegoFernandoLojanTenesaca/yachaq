"""Cuando una pregunta no cabe en un agente: repartirla.

**Esto salió de un fallo medido, no de que quedara bonito.** Preguntado «quiero
fotografiar colibríes cerca de Quito, ¿cuáles hay y dónde?», el agente de una
sola cabeza consultaba las especies **una por una** y agotaba las vueltas con
seis todavía sin mirar. Su propia respuesta lo confesaba: «especies que aún no
he consultado».

No es que le falten vueltas. Es que catorce consultas independientes en serie
son catorce esperas a la red puestas en fila, y ninguna depende de la anterior.
Medido con seis especies: **11,7 s en serie contra 1,9 s en paralelo**.

Así que el coordinador no reparte «para escalar», reparte porque la pregunta ya
venía partida:

    coordinador  ->  ¿esto son varias preguntas sobre cosas distintas?
                     si no, responde el agente normal y aquí no ha pasado nada
                     si sí, una tarea por cosa
    ayudantes    ->  cada uno con su especie, todos a la vez, herramientas propias
    redactor     ->  junta lo que trajeron en una sola respuesta

**El caso más común sigue siendo una sola pregunta**, y para ese esto no debe
existir: partir «¿qué come el hoatzin?» en subtareas costaría tres llamadas al
modelo para responder lo que una respondía. Por eso el coordinador puede decir
que no hay nada que repartir, y entonces esto se aparta.

Los ayudantes no comparten conversación ni memoria: cada uno mira lo suyo y
devuelve lo que encontró. Lo que no traigan no se rellena -es la misma regla del
resto del proyecto, y con varios agentes importa más, porque un hueco entre
tantos datos se nota todavía menos.
"""

import concurrent.futures as futuros
import json
import sys

from agente import CASCADA, SISTEMA, Yachaq

# Más de esto y las tareas dejan de ser «las especies de la pregunta» para ser
# una lista inventada por el modelo. Además GBIF es de otros: doce peticiones a
# la vez es usar su API, cincuenta es maltratarla.
MAXIMO_TAREAS = 8

# Cuántos ayudantes trabajan de verdad a la vez. Ocho lanzados de golpe hacen
# que Groq y Google respondan 429 «limitado por ritmo» a la mitad, y con las
# cuentas gratuitas ese es el techo real, no la CPU. Tres a la vez sigue siendo
# tres veces más rápido que la fila y no dispara el límite.
A_LA_VEZ = 3

REPARTIDOR = """Eres el coordinador de un equipo que responde sobre naturaleza
del Ecuador. Tu único trabajo es partir la pregunta en tareas independientes y
devolver JSON. No respondas la pregunta.

Se parte cuando pide lo mismo sobre VARIAS cosas: varias especies, varios sitios
o una comparación. Cada tarea tiene que poder resolverse sola, sin esperar a
otra.

**Si la pregunta no nombra las especies pero pide un grupo -«colibríes cerca de
Quito», «aves de Galápagos»-, usa `especies_que_conozco` para ver cuáles hay y
haz una tarea por cada una que encaje.** Ahí es donde más falta hace repartir:
sin esto, un solo agente las consulta en fila y se queda a medias.

NO se parte una pregunta sobre una sola cosa, por larga que sea. Partirla cuesta
más de lo que ahorra.

Devuelve exactamente esto y nada más:
{"repartir": false}
o
{"repartir": true, "tareas": ["...", "..."], "encargo": "qué hay que redactar al final"}

Cada tarea se la queda un ayudante que NO ha visto la pregunta original, así que
escríbela entera: «averigua en qué provincias del Ecuador se ha registrado el
colibrí cobrizo (Aglaeactis cupripennis), acotando a Pichincha, y a qué altura»,
no «el cobrizo»."""

AYUDANTE = SISTEMA + """

Trabajas para un coordinador, no para una persona. Consulta lo tuyo y devuelve
solo los datos, en pocas líneas y sin saludar ni cerrar. Si una herramienta no
trae algo, escribe «sin dato» y sigue: otro juntará esto con lo demás y un hueco
inventado aquí se propaga sin que nadie lo vea."""

REDACTOR = """Eres quien redacta la respuesta final sobre naturaleza del
Ecuador. Te llegan los hallazgos de varios ayudantes, cada uno con su parte.

Júntalos en UNA respuesta ordenada. Si son varias especies o sitios comparables,
usa una tabla.

Todo lo que escribas tiene que venir de los hallazgos, y todos los hallazgos
tienen que aparecer: si llegan ocho especies, la tabla lleva ocho filas.

Distingue tres cosas que no son la misma:
- el ayudante trajo el dato -> ponlo;
- el ayudante puso «sin dato» -> escribe que GBIF no lo trae;
- el ayudante NO PUDO CONSULTAR -> escribe «no se pudo consultar», que no es lo
  mismo que no existir.

No completes ninguna de las tres de memoria: es la diferencia entre esta
respuesta y una inventada. Si dos ayudantes se contradicen, dilo en vez de
elegir.

Español, directo, sin preámbulo."""


def _json_de(texto):
    """El JSON que venga dentro de la respuesta, aunque traiga adornos.

    Los modelos abiertos envuelven el JSON en ```json a pesar de que se les pida
    que no. Recortar por las llaves es más corto que pelearse con el prompt.
    """
    try:
        return json.loads(texto[texto.index("{"):texto.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {"repartir": False}


def _preguntar(sistema, mensaje, vueltas=4, empezar_por=None):
    """Un agente de usar y tirar, con su propio hilo de conversación.

    `empezar_por` reparte los ayudantes entre proveedores. Sin eso, los tres
    salían a la vez contra el primero de la cascada, los tres recibían 429 y
    los tres quemaban el mismo escalón: la cascada acababa agotada por el ritmo
    que ella misma provocaba, no por una caída real.
    """
    a = Yachaq(usuario="equipo", proveedor=empezar_por)
    a.historia[0] = {"role": "system", "content": sistema}
    return a.responder(mensaje, vueltas_maximas=vueltas)


def responder(pregunta, usuario="anonimo", conversacion=None):
    """La pregunta, repartida si hace falta y contestada por uno si no.

    Devuelve lo mismo que `Yachaq.responder` más `tareas`, para que se vea si
    hubo equipo y en qué se repartió. Que se vea no es un detalle: es la misma
    razón por la que se enseñan las herramientas usadas.
    """
    # Con vueltas, no sin ellas: para repartir «colibríes cerca de Quito» hay
    # que mirar antes qué colibríes hay, y eso es una llamada a herramienta.
    plan = _json_de(_preguntar(REPARTIDOR, pregunta, vueltas=2)["respuesta"] or "")
    tareas = plan.get("tareas", [])[:MAXIMO_TAREAS] if plan.get("repartir") else []

    # Una sola tarea no es un equipo: es el agente normal con dos llamadas de
    # más. Se cae al camino de siempre, con su memoria y su conversación.
    if len(tareas) < 2:
        salida = Yachaq(usuario=usuario, conversacion=conversacion).responder(pregunta)
        return {**salida, "tareas": []}

    # Aquí está todo el beneficio: las esperas a la red se solapan en vez de
    # ponerse en fila. Pero de tres en tres, no todas de golpe: el límite lo
    # pone la cuota del proveedor, no la máquina.
    #
    # `submit` y no `map`: map() vuelve a lanzar la excepción del primer
    # ayudante que reviente y con ella se pierden las respuestas de todos los
    # demás, aunque siete de ocho hubieran ido bien. Un ayudante que falla es un
    # hueco en la tabla, no una pregunta perdida.
    # Cada ayudante empieza por un proveedor distinto, rotando la cascada. Los
    # demás le siguen quedando de red, así que uno caído no le cuesta nada.
    with futuros.ThreadPoolExecutor(max_workers=min(A_LA_VEZ, len(tareas))) as piscina:
        lanzadas = [piscina.submit(_preguntar, AYUDANTE, t, 4,
                                   CASCADA[i % len(CASCADA)])
                    for i, t in enumerate(tareas)]
        hallazgos = []
        for f in lanzadas:
            try:
                hallazgos.append(f.result())
            except Exception as err:
                hallazgos.append({"respuesta": "", "herramientas": [],
                                  "error": f"{type(err).__name__}: {err}"})

    usadas = [u for h in hallazgos for u in h["herramientas"]]

    # Un ayudante que se cayó NO es un dato que no existe, y confundirlos es el
    # peor fallo posible aquí: la tabla final diría «no hay dato» de una especie
    # que sí tiene registros, y eso no se distingue de un dato consultado.
    material = "\n\n".join(
        # `or`: un ayudante puede volver sin error y con la respuesta a None -el
        # modelo devolvió solo llamadas a herramientas y se quedó sin vueltas-, y
        # concatenar None revienta la pregunta entera.
        f"### {t}\n" + ((h["respuesta"] or "(este ayudante volvió vacío)")
                        if not h.get("error") else
                        "ESTE AYUDANTE NO PUDO CONSULTAR (se cayó el proveedor). "
                        "No es que falte el dato: es que no se miró. Dilo así.")
        for t, h in zip(tareas, hallazgos))
    final = _preguntar(REDACTOR,
                       f"Pregunta original: {pregunta}\n\n"
                       f"{plan.get('encargo', '')}\n\nHallazgos:\n\n{material}",
                       vueltas=0)

    return {"respuesta": final["respuesta"], "herramientas": usadas,
            "tareas": tareas, "proveedor": final.get("proveedor")}


def prueba():
    """Que reparta lo que se puede repartir y NO lo que no.

    Las dos mitades importan igual. Un coordinador que parte siempre triplica el
    coste de «¿qué come el hoatzin?» sin mejorarlo en nada.
    """
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    una = responder("¿qué come el hoatzin?")
    assert not una["tareas"], f"partió una pregunta simple: {una['tareas']}"
    assert una["respuesta"], "se quedó sin responder lo fácil"

    # Un ayudante que revienta no puede llevarse a los otros: con `map` la
    # excepción se propagaba y se perdían las siete respuestas buenas.
    with futuros.ThreadPoolExecutor(3) as piscina:
        lanzadas = [piscina.submit(lambda n=n: 1 // n) for n in (1, 0, 2)]
        vivos = sum(1 for f in lanzadas if not f.exception())
    assert vivos == 2, "una tarea rota se lleva a las demás"

    varias = responder("compara la iguana marina, la iguana terrestre y la "
                       "tortuga gigante de Galápagos: dónde viven y qué comen")
    assert len(varias["tareas"]) >= 2, f"no repartió: {varias['tareas']}"
    assert len(varias["herramientas"]) >= 2, varias["herramientas"]
    texto = varias["respuesta"].lower()
    assert "amblyrhynchus" in texto or "iguana marina" in texto, varias["respuesta"][:300]

    # Que reparta no basta: hay que ver que el material de TODOS los ayudantes
    # llega a la respuesta. Con ocho a la vez, los que caían por rate limit
    # salían en la tabla como «no hay dato», que se lee igual que un dato
    # consultado.
    grupo = responder("¿qué colibríes de los que conoces hay en Pichincha?")
    assert len(grupo["tareas"]) >= 3, f"no repartió el grupo: {grupo['tareas']}"
    nombrados = sum(t.split("(")[-1].split(")")[0].split()[0] in grupo["respuesta"]
                    for t in grupo["tareas"] if "(" in t)
    assert nombrados >= len(grupo["tareas"]) - 1, \
        f"solo {nombrados} de {len(grupo['tareas'])} llegaron a la respuesta final"

    print(f"ok · la simple la resuelve uno · la comparación se repartió en "
          f"{len(varias['tareas'])} tareas · el grupo en {len(grupo['tareas'])}, "
          f"y {nombrados} llegaron enteras a la respuesta")


if __name__ == "__main__":
    prueba() if "--comprobar" in sys.argv else print(
        json.dumps(responder(" ".join(sys.argv[1:]) or
                             "quiero fotografiar colibríes cerca de Quito, ¿cuáles hay y dónde?"),
                   ensure_ascii=False, indent=2))
