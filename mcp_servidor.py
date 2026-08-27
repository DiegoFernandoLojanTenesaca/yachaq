"""Las herramientas de Yachaq, desde cualquier cliente MCP.

MCP es el mismo trato que ya hace este proyecto con Groq —«aquí tienes unas
herramientas, llámalas si te sirven»— pero hablado en un protocolo que entienden
Claude Code, Claude Desktop y los demás. El modelo que decide deja de ser el de
la cascada y pasa a ser el del cliente; el resto no cambia.

**Este fichero no declara ninguna herramienta.** Registra las del `CATALOGO`, y
el SDK saca el nombre, la descripción y el esquema de la firma y del docstring
de cada función, que es exactamente lo que ya hacía `herramientas.esquemas()`
para la API de OpenAI. Los dos caminos leen la misma fuente, así que no hay dos
listas que puedan separarse: ese era el error a evitar y desaparece solo.

Lo que sí cambia es quién eres. Por el stdio de MCP no viaja un usuario, así que
la memoria se guarda bajo lo que diga YACHAQ_USUARIO, o «mcp» si no dice nada.
Sin eso, todo el que conecte comparte los mismos recuerdos.

    python mcp_servidor.py            # habla por stdin/stdout, no imprime nada

En Claude Code:

    claude mcp add yachaq -- <ruta>/.venv/Scripts/python <ruta>/mcp_servidor.py
"""

import os
import pathlib
import sys

from mcp.server import MCPServer

import herramientas
import memoria

servidor = MCPServer(
    "yachaq",
    version="0.3",
    instructions="Naturaleza del Ecuador con datos, no de memoria: identifica "
                 "especies de una foto con un modelo entrenado, consulta los "
                 "registros reales de GBIF y busca en fichas de 100 especies. "
                 "Cuando una herramienta no trae un dato, no lo rellenes.",
)

for _funcion in herramientas.CATALOGO.values():
    servidor.add_tool(_funcion)


async def _comprobar():
    """Conectarse como se conectaría Claude Code: otro proceso, por stdio.

    Llamar a `servidor.list_tools()` desde aquí dentro no probaría nada de MCP:
    probaría que existe un diccionario. Lo que puede romperse está en el medio
    -que los esquemas sobrevivan a serializarse, que el proceso arranque sin
    imprimir nada en stdout, que un fallo vuelva como resultado y no cierre la
    conexión-, así que la comprobación levanta el servidor de verdad y le habla
    el protocolo.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parametros = StdioServerParameters(
        command=sys.executable, args=[str(pathlib.Path(__file__).resolve())],
        env={**os.environ, "YACHAQ_USUARIO": "comprobacion"})

    async with stdio_client(parametros) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            info = await sesion.initialize()
            listadas = (await sesion.list_tools()).tools

            assert {t.name for t in listadas} == set(herramientas.CATALOGO), \
                "MCP y el agente ven herramientas distintas"
            assert all(t.description and t.input_schema for t in listadas), \
                "alguna herramienta va sin descripción o sin esquema"

            # Un `required` perdido por el camino no da error: deja al cliente
            # adivinando qué argumento es obligatorio y llamando mal.
            donde = next(t for t in listadas if t.name == "donde_se_ha_visto")
            assert donde.input_schema["required"] == ["especie"], donde.input_schema
            assert "Cajas" in donde.description, "el docstring no llegó entero al cliente"

            r = await sesion.call_tool("buscar_especie", {"nombre": "piquero patiazul"})
            assert "Sula nebouxii" in r.content[0].text, r.content[0].text[:300]

            # Un fallo tiene que volver como resultado. Si se propagara como
            # excepción por stdio, cerraría la sesión entera del cliente.
            malo = await sesion.call_tool("buscar_especie", {"nombre": "kdjfhskjdfh"})
            assert "false" in malo.content[0].text.lower(), malo.content[0].text[:200]

            return info.server_info.name, len(listadas)


def prueba():
    import asyncio
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    nombre, cuantas = asyncio.run(_comprobar())
    print(f"ok · conectado a «{nombre}» por stdio · {cuantas} herramientas, "
          f"las mismas del agente · un nombre inventado no tumba la sesión")


if __name__ == "__main__":
    if "--comprobar" in sys.argv:
        prueba()
    else:
        memoria.USUARIO.set(os.environ.get("YACHAQ_USUARIO", "mcp"))
        servidor.run("stdio")
