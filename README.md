<div align="center">

<img src="icono.svg" width="88" alt="Yachaq">

# Yachaq

**Agente de herramientas sobre [Riksi](https://github.com/DiegoFernandoLojanTenesaca/riski).**

*Riksi* (kichwa: *el que reconoce*) clasifica una foto en el navegador.
*Yachaq* (*el que sabe*) pone ese clasificador detrás de un agente que además
consulta GBIF, busca en fichas y recuerda con quién habla.

</div>

---

## Qué es

Riksi es un modelo de 3,8 MB que responde una pregunta: *¿qué especie es esta?*
Yachaq lo convierte en una herramienta más de un agente que decide, por sí solo,
qué consultar para responder lo que le preguntes.

```
> ¿qué es esto? la foto está en C:\fotos\ave.jpg

   [identificar]        modelo de Riksi, 100 especies
   [donde_se_ha_visto]  GBIF, registros reales en Ecuador

Es un colibrí cobrizo (Aglaeactis cupripennis), con 83 % de confianza.
Registrado sobre todo en Pichincha y Azuay, entre 2.400 y 4.100 m.
```

| | Riksi | Yachaq |
|---|---|---|
| Dónde corre | navegador, sin servidor | API, Docker o MCP |
| Qué hace | clasifica una foto | decide qué consultar y lo junta |
| Datos | 100 especies + 74 aves por canto | + GBIF en vivo + 691 fichas |
| Conexión | funciona sin internet | necesita red y un LLM |

## Arquitectura

```
                      ┌──────────────────────────────┐
   FastAPI ─────────► │  cascada de 6 proveedores    │
   MCP     ─────────► │  (el primero que responda)   │
   equipo  ─────────► └───────────────┬──────────────┘
                                      │ tool calls
   ┌──────────────────────────────────┴───────────────────────────┐
   │ identificar          ONNX · modelo de Riksi · 100 especies    │
   │ buscar_especie       GBIF · resolución de nombres             │
   │ donde_se_ha_visto    GBIF · registros por provincia y lugar   │
   │ consultar_fichas     RAG · 691 párrafos · fastembed + numpy   │
   │ especies_que_conozco catálogo del modelo                      │
   │ recordar / olvidar   SQLite · memoria entre conversaciones    │
   └───────────────────────────────────────────────────────────────┘
```

Los tres frentes —API, MCP y equipo— comparten el mismo `CATALOGO`. El esquema
que ve el modelo se genera de la firma y el docstring de cada función: un
esquema escrito aparte es una segunda fuente de verdad que se desincroniza en
cuanto alguien añade un argumento, y el fallo aparece como un modelo que llama
mal, no como un error.

## Proveedores

Seis servicios hablan el dialecto de OpenAI, así que el mismo código sirve para
todos y la cascada pasa al siguiente cuando uno se cae, **con la misma
conversación**: lo ya consultado no se repite.

| # | proveedor | modelo | notas |
|---|---|---|---|
| 1 | groq | `openai/gpt-oss-120b` | el más rápido |
| 2 | mistral | `mistral-medium-latest` | `large` da timeout |
| 3 | cohere | `command-a-03-2025` | |
| 4 | openrouter | `openai/gpt-oss-120b` | los `:free` dan 404 |
| 5 | nvidia | `deepseek-ai/deepseek-v4-flash-0731` | 5xx intermitente, reintenta |
| 6 | google | `gemini-2.5-flash` | último: cuota diaria corta |

La tabla sale de probarlos, no de sus webs. Lo que se mide no es que la clave
valga sino que el modelo **llame a herramientas**, que es lo único que necesita
un agente y no figura en ninguna documentación:

```bash
python agente.py --probar      # vuelve a medirlo en 20 s
python agente.py --comprobar   # rompe el primero y verifica que la cascada aguanta
```

Descartados y por qué: `cerebras` y `sambanova` responden 402 con saldo 0;
`ai21` devuelve 410, API retirada. Se quedan en el código fuera de la cascada —
lo que se probó y no sirve evita reintentarlo dentro de tres meses.

Quien responde se dice al pie: *«lo respondió mistral; groq (modelo no servido),
google (limitado por ritmo) no estaban disponibles»*.

## RAG

691 párrafos de 82 especies: Wikipedia en español como cuerpo, descripciones de
GBIF ≥ 80 caracteres como complemento (de veinte entradas, una pasaba de cien
caracteres; el resto son etiquetas de una palabra).

**Sin base de datos, a propósito.** 691 vectores × 384 dimensiones son once
megas en memoria; compararlos con una multiplicación de matrices tarda menos que
el viaje de ida y vuelta a un Postgres en la misma máquina. pgvector gana cuando
los vectores no caben en RAM, cuando varios procesos escriben, o cuando hay que
filtrar por metadatos antes de buscar. Entonces cambia un fichero.

Embeddings sobre **ONNX**, el mismo motor que ya usa Riksi: 220 MB frente a los
2,5 GB de PyTorch.

**El corte de similitud está medido.** Con un 0,35 puesto a ojo, *«¿el volcán
más alto de Marte?»* devolvía párrafos de un colibrí con 0,40. Catorce preguntas
—ocho con respuesta en las fichas, seis sin ella— dan dos poblaciones que no se
solapan:

| | peor | mejor |
|---|---|---|
| con respuesta | 0,476 | 0,771 |
| sin respuesta | 0,268 | 0,401 |

Corte en **0,44**, el punto medio del hueco. `python indice.py --calibrar` lo
recalcula y avisa si algún día se solapan.

## Memoria

Dos cosas distintas: la **conversación** (el hilo de una sesión) y los
**recuerdos** (hechos sobre quien pregunta, válidos en cualquier conversación
futura). Guardar el transcript entero como memoria a largo plazo crece sin tope
y llena el contexto de charla, así que los recuerdos los extrae el modelo con la
herramienta `recordar`.

En SQLite, no Postgres: está en la stdlib, escribe con WAL y aguanta un servidor
de un proceso. Postgres el día que haya réplicas escribiendo a la vez.

- **Deduplicado por significado.** «Me gustan los colibríes» y «me interesan
  mucho los colibríes» son el mismo recuerdo; por encima de 0,85 no entra.
- **Inspeccionable y borrable.** `GET /memoria/{usuario}` y `DELETE`.
- **El usuario no es argumento de las herramientas.** Si lo fuera, el modelo
  podría escribir otro nombre y leer la memoria de alguien más. Falta
  autenticación: hoy quien acierte un nombre lee esa memoria.
- **Una conexión por hilo.** Con una compartida, los tres ayudantes del
  multi-agente leían a la vez y sqlite3 devolvía *«bad parameter or other API
  misuse»*. El candado protegía las escrituras y las lecturas iban sueltas;
  extenderlo habría serializado justo lo que se paraleliza. La prueba abre seis
  hilos, que es lo que faltaba para que el fallo apareciera.

## Multi-agente

El plan decía «uno identifica, otro verifica». Medido sobre las 200 fotos del
banco de validación de Riksi:

| | |
|---|---|
| responde con seguridad | 197 (193 bien, 4 mal) |
| no está seguro | 3 |
| …con la correcta entre las tres candidatas | **0** |

Un verificador no habría cambiado ninguna respuesta. Lo que sí falla es el
volumen: *«colibríes cerca de Quito»* consultaba especies en fila hasta agotar
las vueltas. Seis consultas independientes tardan **11,7 s en serie y 1,9 s en
paralelo**.

```
coordinador → ¿varias cosas distintas? si no, responde el agente normal
ayudantes   → una especie cada uno, 3 a la vez
redactor    → junta los hallazgos en una tabla
```

Dos fallos que solo aparecen con varios agentes:

- El coordinador **no repartía lo que más falta hacía**, porque la pregunta no
  nombra las especies. Ahora consulta el catálogo primero: de 0 tareas a 8.
- **Ocho ayudantes a la vez tumbaban al proveedor** (429). Los caídos volvían
  vacíos y el redactor los escribía como *«no hay dato»* — indistinguible de un
  dato consultado. Tres a la vez, y un ayudante caído llega como *«no se pudo
  consultar»*, que es una tercera cosa. De 5 de 8 a **8 de 8**.

## MCP

Las mismas siete herramientas desde cualquier cliente MCP. **El servidor no
contiene ningún LLM**: no importa `openai`, no lee claves, no hay cascada
dentro. Expone funciones y espera; el modelo lo pone quien conecte.

```bash
claude mcp add yachaq -- <ruta>/.venv/Scripts/python <ruta>/mcp_servidor.py
python mcp_servidor.py --comprobar
```

La comprobación **levanta el servidor en otro proceso y le habla el protocolo**.
Llamar a `list_tools()` desde dentro solo probaría que existe un diccionario; lo
que puede romperse está en el medio, y sobre todo que un fallo vuelva como
resultado: por stdio una excepción no estropea una petición, cierra la sesión
del cliente.

## Correrlo

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt

python fichas.py && python fichas.py --indexar   # una vez: baja y vectoriza
python agente.py --usuario=diego                 # consola
uvicorn servidor:app --reload                    # API en :8000/docs
```

| endpoint | |
|---|---|
| `POST /preguntar` | pregunta; devuelve respuesta **y qué herramientas usó**. `"equipo": true` la reparte |
| `POST /identificar` | sube una foto y pregunta por ella |
| `GET /salud` | estado, proveedores, herramientas |
| `GET`/`DELETE /memoria/{usuario}` | ver y borrar lo que recuerda de ti |
| `DELETE /conversacion/{id}` | olvidar una conversación |

```bash
docker build -t yachaq .
docker run -p 8000:8000 --env-file .env -v yachaq-datos:/datos yachaq
```

El modelo, las fichas y los vectores van dentro de la imagen. La memoria en un
volumen: dentro se borraría al recrear el contenedor.

Las claves van en `.env`, que está en el `.gitignore`.

## Comprobaciones

Cada módulo comprueba lo único que tiene lógica propia. Ninguna necesita
framework:

```bash
python herramientas.py               # GBIF, esquemas, filtro por lugar
python memoria.py                    # no duplica, borra lo que toca
python indice.py                     # busca por significado
python indice.py --calibrar          # recalcula el corte
python agente.py --probar            # qué proveedor sirve hoy
python agente.py --comprobar         # la cascada aguanta una caída
python mcp_servidor.py --comprobar   # MCP por stdio, de verdad
python equipo.py --comprobar         # reparte lo repartible y nada más
```

## Decisiones

**Se enseña qué herramientas se usaron.** No es depuración: es la diferencia
entre un agente que consultó los registros y uno que se lo inventó.

**Los errores de herramienta vuelven como texto, no como excepción.** El modelo
puede leerlos y corregir; una excepción mataría la conversación.

**No rellenar los huecos es una regla.** La primera versión respondía *«la
herramienta no devolvió datos de altitud, pero la especie se registra
habitualmente entre 2.000 y 3.500 m»*. Eso es memoria disfrazada de consulta, en
el mismo párrafo y con el mismo tono que los datos reales. Arreglado por los dos
lados: la herramienta ya no devuelve `null` —un hueco invita a rellenarlo— sino
*«GBIF no trae la altura en estos registros. No hay dato: no lo inventes»*.

**El nombre común es una puerta de atrás a la especie equivocada.** El buscador
de GBIF está hecho para nombres científicos:

| se busca | GBIF devuelve | |
|---|---|---|
| `piquero patiazul` | nada, **con confianza 100** | un no-match que parece fiable |
| `colibri cobrizo` | el género `Colibri`, confianza 92 | **un género colado como especie** |

La primera versión aceptaba eso y el agente respondió sobre un *Sterna trudeaui*
que nadie mencionó. Ahora solo se acepta coincidencia exacta de nombre
científico; si no, se busca en el índice de vernáculos, que sí devuelve *Sula
nebouxii*. El caso está en la comprobación: un fallo así no da error, da otra
especie.

**El agente no deduce geografía.** Respondió *«Pichincha, donde está Cajas»* —el
Cajas está en Azuay— cruzando de memoria el nombre del parque con la provincia
más frecuente. `donde_se_ha_visto` acepta ahora un `lugar` y GBIF filtra por él:
ya no deduce dónde queda, pregunta qué se ha visto allí.

**El agente hereda el «no lo sé» de Riksi.** Cuando la confianza no llega al
umbral calibrado, lo dice. En el campo, una identificación equivocada dada con
seguridad hace más daño que ninguna.

## Estado

- [x] **1** API · FastAPI, Docker, conversación
- [x] **2** Herramientas · el modelo decide cuándo identificar, buscar o consultar
- [x] **3** RAG · 691 párrafos, corte medido
- [x] **4** Memoria · SQLite, servidor sin estado
- [x] **5** MCP · las mismas herramientas desde cualquier cliente
- [x] **6** Multi-agente · coordinador, ayudantes en paralelo, redactor

## Licencia

Código bajo MIT. El modelo viene de [Riksi](https://github.com/DiegoFernandoLojanTenesaca/riski)
bajo CC-BY 4.0; los datos de observaciones son de [GBIF](https://www.gbif.org) y
las fichas de Wikipedia en español (CC-BY-SA).
