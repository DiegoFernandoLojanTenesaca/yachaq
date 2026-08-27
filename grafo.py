"""El mismo equipo de `equipo.py`, escrito como un grafo de LangGraph.

**Existe para compararlo, no para sustituirlo.** `equipo.py` hace esto mismo en
46 sentencias y sin dependencias; LangGraph trae 18 paquetes. Un cambio así hay
que justificarlo con lo que aporta, y aquí aporta exactamente una cosa que la
versión a mano no tiene: **checkpoints**.

Una pregunta repartida en ocho ayudantes son ocho consultas a GBIF y ocho
llamadas al modelo. Si el proceso se cae en el séptimo, la versión a mano lo
repite todo. Con el checkpointer en SQLite, LangGraph guarda el estado después
de cada nodo y al reanudar con el mismo `thread_id` sigue por donde iba. Eso no
es azúcar: es no volver a pagar lo ya pagado.

Lo demás lo hereda: las mismas herramientas, la misma cascada de proveedores y
los mismos prompts de `equipo.py`. Lo que cambia es quién lleva el control de
flujo.

    repartir ──► Send(uno por tarea) ──► ayudante ×N ──► redactar
        │                                                   ▲
        └────────── sin tareas ──► responder_solo ──────────┘

`Send` es el fan-out dinámico: el número de ayudantes no se sabe al construir el
grafo, sale de lo que decida el coordinador en tiempo de ejecución.

    python grafo.py --comprobar
    python grafo.py "compara la iguana marina y la terrestre"
"""

import json
import operator
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

from agente import CASCADA, Yachaq
from equipo import AYUDANTE, MAXIMO_TAREAS, REDACTOR, REPARTIDOR, _json_de, _preguntar

AQUI = Path(__file__).parent
CHECKPOINTS = AQUI / "grafo.db"


class Estado(TypedDict):
    """Lo que viaja por el grafo.

    `hallazgos` y `usadas` llevan `operator.add` porque los escriben varios
    ayudantes a la vez: sin el reductor, el último en terminar pisaría a los
    demás y la respuesta saldría con una sola especie.
    """
    pregunta: str
    tareas: list[str]
    encargo: str
    hallazgos: Annotated[list[dict], operator.add]
    usadas: Annotated[list[dict], operator.add]
    respuesta: str
    proveedor: str


def repartir(estado: Estado) -> dict:
    """Decide si la pregunta se parte, y en qué."""
    salida = _preguntar(REPARTIDOR, estado["pregunta"], vueltas=2)
    plan = _json_de(salida["respuesta"] or "")
    tareas = plan.get("tareas", [])[:MAXIMO_TAREAS] if plan.get("repartir") else []
    # Una sola tarea no es un equipo: es el agente normal con dos llamadas de
    # más. Igual que en la versión a mano.
    return {"tareas": tareas if len(tareas) >= 2 else [],
            "encargo": plan.get("encargo", "")}


def _rama(estado: Estado):
    """Un `Send` por tarea, o el camino corto si no hay nada que repartir.

    Aquí está lo que un grafo estático no puede hacer: el número de ramas sale
    de los datos, no del diagrama.
    """
    if not estado["tareas"]:
        return "responder_solo"
    return [Send("ayudante", {"tarea": t, "turno": i})
            for i, t in enumerate(estado["tareas"])]


def ayudante(paquete: dict) -> dict:
    """Una tarea, un agente propio.

    Cada uno empieza por un proveedor distinto: si los tres salen contra el
    primero de la cascada, los tres reciben 429 y la agotan por el ritmo que
    ellos mismos provocan.
    """
    try:
        h = _preguntar(AYUDANTE, paquete["tarea"], vueltas=4,
                       empezar_por=CASCADA[paquete["turno"] % len(CASCADA)])
    except Exception as err:
        h = {"respuesta": "", "herramientas": [],
             "error": f"{type(err).__name__}: {err}"}
    return {"hallazgos": [{"tarea": paquete["tarea"], **h}],
            "usadas": h["herramientas"]}


def redactar(estado: Estado) -> dict:
    """Junta los hallazgos en una respuesta.

    Un ayudante caído NO es un dato que no existe. Confundirlos pondría en la
    tabla «no hay dato» sobre una especie que sí tiene registros, y eso no se
    distingue de un dato consultado.
    """
    # Los ayudantes terminan en cualquier orden, así que se reordenan por la
    # tarea: una tabla que cambia de orden en cada ejecución no se puede leer.
    por_tarea = {h["tarea"]: h for h in estado["hallazgos"]}
    material = "\n\n".join(
        f"### {t}\n" + (por_tarea[t]["respuesta"] if not por_tarea[t].get("error") else
                        "ESTE AYUDANTE NO PUDO CONSULTAR (se cayó el proveedor). "
                        "No es que falte el dato: es que no se miró. Dilo así.")
        for t in estado["tareas"] if t in por_tarea)

    final = _preguntar(REDACTOR,
                       f"Pregunta original: {estado['pregunta']}\n\n"
                       f"{estado['encargo']}\n\nHallazgos:\n\n{material}", vueltas=0)
    return {"respuesta": final["respuesta"], "proveedor": final.get("proveedor")}


def responder_solo(estado: Estado) -> dict:
    """El camino corto: una pregunta simple no necesita equipo."""
    s = Yachaq(usuario="grafo").responder(estado["pregunta"])
    return {"respuesta": s["respuesta"], "usadas": s["herramientas"],
            "proveedor": s.get("proveedor")}


def construir():
    plano = StateGraph(Estado)
    plano.add_node("repartir", repartir)
    plano.add_node("ayudante", ayudante)
    plano.add_node("redactar", redactar)
    plano.add_node("responder_solo", responder_solo)

    plano.add_edge(START, "repartir")
    plano.add_conditional_edges("repartir", _rama, ["ayudante", "responder_solo"])
    plano.add_edge("ayudante", "redactar")
    plano.add_edge("redactar", END)
    plano.add_edge("responder_solo", END)
    return plano


def responder(pregunta, hilo="suelto", checkpoints=True):
    """La pregunta por el grafo. `hilo` es la clave para reanudar.

    Reanudar es el motivo de que este fichero exista: con el mismo `hilo`, lo
    que ya se consultó no se vuelve a consultar.
    """
    plano = construir()
    if not checkpoints:
        salida = plano.compile().invoke({"pregunta": pregunta},
                                        {"configurable": {"thread_id": hilo}})
        return _formato(salida, reanudado=False)

    # `check_same_thread=False` porque los ayudantes corren en hilos distintos y
    # todos escriben su checkpoint.
    cx = sqlite3.connect(CHECKPOINTS, check_same_thread=False)
    try:
        grafo = plano.compile(checkpointer=SqliteSaver(cx))
        config = {"configurable": {"thread_id": hilo}}
        antes = grafo.get_state(config)

        # Un `invoke` con entrada nueva arranca otra ejecución aunque el hilo ya
        # exista: el checkpointer guarda el estado, no decide por ti si hay que
        # seguir. Así que se mira antes.
        #
        #   - hilo terminado (`next` vacío y con respuesta) -> ya está hecho;
        #   - hilo a medias (`next` con nodos) -> se sigue por donde iba,
        #     invocando con None en vez de con la pregunta;
        #   - hilo nuevo -> se ejecuta entero.
        if antes.values.get("respuesta") and not antes.next:
            return _formato(antes.values, reanudado=True)

        entrada = None if antes.next else {"pregunta": pregunta}
        return _formato(grafo.invoke(entrada, config), reanudado=bool(antes.next))
    finally:
        cx.close()


def _formato(estado, reanudado):
    return {"respuesta": estado.get("respuesta"),
            "herramientas": estado.get("usadas", []),
            "tareas": estado.get("tareas", []),
            "proveedor": estado.get("proveedor"),
            "reanudado": reanudado}


def prueba():
    """Que el grafo haga lo mismo que la versión a mano, y que reanude.

    Lo segundo es lo único que no sabe hacer `equipo.py`, así que es lo que hay
    que comprobar de verdad: reanudar un hilo terminado no puede volver a
    llamar a las herramientas.
    """
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    CHECKPOINTS.unlink(missing_ok=True)

    una = responder("¿qué come el hoatzin?", hilo="simple")
    assert not una["tareas"], f"partió una pregunta simple: {una['tareas']}"
    assert una["respuesta"], "se quedó sin responder lo fácil"

    varias = responder("compara la iguana marina, la iguana terrestre y la "
                       "tortuga gigante de Galápagos: dónde viven y qué comen",
                       hilo="comparacion")
    assert len(varias["tareas"]) >= 2, f"no repartió: {varias['tareas']}"
    assert len(varias["herramientas"]) >= 2, varias["herramientas"]
    assert "iguana marina" in varias["respuesta"].lower(), varias["respuesta"][:300]

    # Reanudar: mismo hilo, el estado está en disco y no se repite ni una
    # consulta. En la versión a mano esto cuesta las ocho llamadas otra vez.
    otra_vez = responder("compara la iguana marina, la iguana terrestre y la "
                         "tortuga gigante de Galápagos: dónde viven y qué comen",
                         hilo="comparacion")
    assert otra_vez["reanudado"], "no reanudó: volvió a ejecutar el grafo entero"
    assert otra_vez["respuesta"] == varias["respuesta"], "reanudó con otra respuesta"

    # Y un hilo distinto sí ejecuta: el checkpoint no puede devolver lo de otro.
    nuevo = responder("¿dónde vive el hoatzin?", hilo="otro")
    assert not nuevo["reanudado"] and nuevo["respuesta"] != varias["respuesta"], nuevo

    print(f"ok · la simple la resuelve un nodo · la comparación en "
          f"{len(varias['tareas'])} ramas y {len(varias['herramientas'])} consultas · "
          f"reanudar el hilo no repite ninguna")
    CHECKPOINTS.unlink(missing_ok=True)


if __name__ == "__main__":
    if "--comprobar" in sys.argv:
        prueba()
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(json.dumps(responder(" ".join(a for a in sys.argv[1:] if not a.startswith("--"))
                                   or "compara la iguana marina y la iguana terrestre"),
                         ensure_ascii=False, indent=2))
