# Yachaq

**Un agente de naturaleza del Ecuador que usa herramientas de verdad.**

*Yachaq*, en kichwa, es el que sabe. Le preguntas en lenguaje natural y él
decide qué hacer: identificar una foto con el modelo de
[Riksi](https://github.com/DiegoFernandoLojanTenesaca/riski), buscar la especie
en GBIF, o mirar dónde se ha registrado en el país.

No es un chatbot con un prompt bonito. Cuando dice que un ave es un colibrí
cobrizo es porque pasó la foto por un clasificador; cuando dice dónde vive es
porque consultó los registros reales.

```
> ¿qué es esto? la foto está en C:\fotos\ave.jpg

   [identificar({'ruta_de_la_foto': 'C:\\fotos\\ave.jpg'})]
   [donde_se_ha_visto({'especie': 'Aglaeactis cupripennis'})]

Es un colibrí cobrizo (Aglaeactis cupripennis), con un 83 % de confianza.
En Ecuador está registrado sobre todo en Pichincha y Azuay, entre los
2.400 y los 4.100 metros: es un ave de páramo y bosque altoandino.
```

## Las herramientas

| Herramienta | Qué hace de verdad |
|---|---|
| `identificar` | Corre el modelo de Riksi (100 especies, 3,8 MB) sobre la foto |
| `buscar_especie` | Resuelve el nombre en GBIF y cuenta sus fotos en Ecuador |
| `donde_se_ha_visto` | Agrupa las observaciones reales por provincia y da el rango de altura |
| `consultar_fichas` | Busca por significado en 691 párrafos de Wikipedia y GBIF |

**El esquema que ve el modelo se genera de las firmas de las funciones**, no se
escribe aparte. Un esquema escrito a mano es una segunda fuente de verdad que
se desincroniza en cuanto alguien añade un argumento, y el fallo aparece como
un modelo que llama mal a la herramienta, no como un error.

## El proveedor es intercambiable

Groq y Cerebras regalan cuota y ambos hablan el mismo dialecto que OpenAI, así
que el mismo código sirve para los dos. Se cambia con una variable de entorno:

```bash
PROVEEDOR=groq      # openai/gpt-oss-120b
PROVEEDOR=cerebras  # gpt-oss-120b
```

Las claves se sacan gratis en [console.groq.com/keys](https://console.groq.com/keys)
y [cloud.cerebras.ai](https://cloud.cerebras.ai), y van en `.env`, que está en el
`.gitignore`. Una clave publicada la revocan, con razón.

**El catálogo publicado no es el catálogo de tu cuenta.** La documentación de
Groq anuncia `llama-3.3-70b-versatile` y esta clave responde 404 al pedirlo: de
los 14 modelos que sirve de verdad, ninguno es un Llama de chat. Antes de fijar
un modelo, pregúntale a la API cuáles tiene:

```bash
python agente.py --modelos
```

**Y la cuota gratuita no siempre está.** Cerebras devuelve 402 con esta cuenta,
así que el respaldo existe en el código pero no en la práctica. Por eso los
fallos del proveedor salen traducidos a una frase accionable en vez de a una
traza: 402 sugiere cambiar de proveedor, 404 manda a `--modelos`, 401 al `.env`.

## Correrlo

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

python fichas.py          # baja las fichas (una vez)
python fichas.py --indexar  # calcula los vectores
python herramientas.py    # comprueba las herramientas y GBIF, sin gastar cuota
python indice.py --calibrar # vuelve a medir el corte de parecido
python agente.py          # conversación por consola
uvicorn servidor:app --reload
```

En `http://127.0.0.1:8000/docs` está la API:

| Endpoint | Qué es |
|---|---|
| `POST /preguntar` | una pregunta; devuelve la respuesta **y qué herramientas usó** |
| `POST /identificar` | sube una foto y pregunta por ella en la misma llamada |
| `GET /salud` | si está vivo, con qué modelo y qué herramientas tiene |
| `DELETE /conversacion/{id}` | olvida una conversación |

Con Docker:

```bash
docker build -t yachaq .
docker run -p 8000:8000 -e GROQ_API_KEY=tu_clave yachaq
```

El modelo de Riksi va **dentro de la imagen**: un contenedor que depende de una
ruta del disco de quien lo construyó no es portátil.

## Decisiones tomadas

**La respuesta enseña qué herramientas se usaron.** No es depuración: es la
diferencia entre un agente que consultó los registros y uno que se lo inventó,
y quien lee la respuesta tiene derecho a distinguirlas.

**El bucle de herramientas tiene tope.** Un modelo puede quedarse pidiendo la
misma herramienta una y otra vez; sin límite eso es una cuota agotada sin que
nadie se entere.

**Los errores de una herramienta vuelven como texto, no como excepción.** El
modelo puede leer el error y corregir; una excepción mataría la conversación.

**No rellenar los huecos es una regla, no un detalle de estilo.** La primera
versión respondía esto cuando GBIF no traía la altura:

> *«la herramienta no devolvió datos de altitud, pero la especie se registra
> habitualmente entre 2.000 y 3.500 m»*

Eso es memoria disfrazada de consulta, y va en el mismo párrafo y con el mismo
tono que los datos reales: quien lee no puede distinguirlos. El arreglo fue por
los dos lados. La herramienta ya no devuelve `null` —un hueco invita a
rellenarlo— sino la frase *«GBIF no trae la altura en estos registros. No hay
dato: no lo inventes»*, y el sistema prohíbe completar de memoria lo que una
herramienta no trajo. Ahora responde: *«no dispongo de ese dato»*.

**El nombre común es la puerta de atrás.** El buscador de nombres de GBIF está
hecho para nombres científicos, y con los comunes miente de dos formas que no
parecen mentiras:

| se busca | GBIF devuelve | |
|---|---|---|
| `piquero patiazul` | nada, **con confianza 100** | un no-match que parece fiable |
| `colibri cobrizo` | el género `Colibri`, confianza 92 | **un género colado como especie** |

La primera versión aceptaba eso y el agente contestó tan tranquilo sobre un
*Sterna trudeaui* que nadie había mencionado. Ahora solo se acepta una
coincidencia **exacta** de nombre científico; si no la hay, se busca en el
índice de nombres vernáculos, que es donde sí vive «piquero patiazul» y devuelve
*Sula nebouxii*. La respuesta dice por qué vía se encontró.

El caso está en la comprobación, con el nombre y todo: un fallo así no da error,
da otra especie.

## El RAG: 691 párrafos y ninguna base de datos

Las fichas salen de **Wikipedia en español** —artículos de 2.700 a 15.000
caracteres, el cuerpo de la ficha— y de las descripciones de GBIF, que son
telegráficas: para el piquero patiazul devuelve «Bandera», «Carnívoro», «LC»,
«Muy especialista». De veinte entradas, una pasaba de cien caracteres. Se
aprovechan esas y se tiran las demás. Quedan 691 fragmentos de 82 especies; 11
no tienen artículo, casi todas plantas.

**Aquí no hay Postgres, y es a propósito.** El plan decía pgvector. Al medirlo,
son 691 vectores de 384 dimensiones: once megas en memoria, y compararlos todos
con una multiplicación de matrices tarda menos que el viaje de ida y vuelta a un
Postgres en la misma máquina. pgvector empieza a ganar cuando los vectores no
caben en memoria, cuando varios procesos escriben a la vez o cuando hay que
filtrar por metadatos antes de buscar. Nada de eso pasa todavía; cuando la
memoria de la fase 4 traiga escrituras concurrentes, esa será la razón, y
entonces solo cambia un fichero.

Los embeddings corren sobre **ONNX**, el mismo motor que ya usa Riksi: 220 MB y
ninguna dependencia nueva de peso. Traerse PyTorch entero para vectorizar mil
párrafos serían dos gigas y medio.

**El corte de parecido está medido, no puesto a ojo.** Con el 0,35 que puse a
mano, «¿cómo se llama el volcán más alto de Marte?» devolvía párrafos de un
colibrí con 0,40 y el modelo los habría usado como si vinieran a cuento. Así que
lo medí con catorce preguntas, ocho que las fichas sí responden y seis que no:

| | peor | mejor |
|---|---|---|
| **con respuesta** en las fichas | 0,476 | 0,771 |
| **sin respuesta** | 0,268 | 0,401 |

Las dos poblaciones no se solapan: hay un hueco limpio entre 0,401 y 0,476, y el
corte va en medio, en **0,44**. Se reproduce con `python indice.py --calibrar`,
que además avisa si algún día se solapan —ahí no habría corte que valga y haría
falta más contexto por fragmento o un modelo de embeddings mejor.

**Devolver nada es una respuesta.** Cuando el RAG no encuentra material, no
devuelve una lista vacía a secas: devuelve la frase *«Ninguna ficha habla de eso.
No hay material: dilo y no lo completes de memoria»*. Preguntado por la abeja
carpintera de Darwin —una de las once sin ficha— el agente busca, no encuentra,
consulta GBIF por si acaso y responde que no tiene el dato, en vez de contar lo
que recuerda de las abejas carpinteras en general.

**El agente hereda el «no lo sé» de Riksi.** El modelo de fotos trae su umbral
calibrado, y cuando la confianza no llega, el agente lo dice en vez de afirmar.
En el campo, una identificación equivocada dada con seguridad hace más daño que
no dar ninguna.

## Plan

- [x] **1 · API**. FastAPI, Docker, endpoint de conversación.
- [x] **2 · Herramientas**. El modelo decide cuándo identificar, buscar o
      consultar registros, y puede pedir varias cosas a la vez.
- [x] **3 · RAG** sobre fichas de especies. 691 párrafos, embeddings en local,
      corte de parecido medido.
- [ ] **4 · Memoria** que sobreviva al reinicio: qué especies te interesan,
      dónde sales al campo.
- [ ] **5 · Servidor MCP** para usar estas herramientas desde Claude Code o
      cualquier otro cliente.
- [ ] **6 · Multi-agente**: uno identifica, otro verifica contra los registros,
      un coordinador decide.

## Licencia

Código bajo MIT. El modelo de identificación viene de Riksi y se publica bajo
CC-BY 4.0; los datos de observaciones son de [GBIF](https://www.gbif.org).
