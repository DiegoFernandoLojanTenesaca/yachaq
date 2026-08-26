"""Una conversación real, para ver si el agente encadena herramientas solo."""
import sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from agente import Yachaq

proveedor = sys.argv[1] if len(sys.argv) > 1 else "groq"
foto = sys.argv[2] if len(sys.argv) > 2 else None

a = Yachaq(proveedor=proveedor)
print(f"=== {a.proveedor} · {a.modelo}\n")

preguntas = ["¿Dónde puedo ver una iguana marina en Ecuador y a qué altura vive?"]
if foto:
    preguntas.append(f"Identifica la foto que está en {foto} y dime dónde vive esa especie.")

for p in preguntas:
    print(f"> {p}")
    salida = a.responder(p)
    for uso in salida["herramientas"]:
        print(f"   [{uso['herramienta']}({uso['argumentos']})]")
    print(f"\n{salida['respuesta']}\n")
