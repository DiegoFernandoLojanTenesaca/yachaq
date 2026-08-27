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
| `especies_que_conozco` | Devuelve el catálogo, para no proponer a ciegas |
| `recordar` / `olvidar` | Guarda y borra lo que le cuentas, entre conversaciones |

**El esquema que ve el modelo se genera de las firmas de las funciones**, no se
escribe aparte. Un esquema escrito a mano es una segunda fuente de verdad que
se desincroniza en cuanto alguien añade un argumento, y el fallo aparece como
un modelo que llama mal a la herramienta, no como un error.

## Seis proveedores, y se cambia solo

Un agente atado a un proveedor gratuito es un agente que no funciona la mitad de
los días. Todos hablan el dialecto de OpenAI, así que el mismo código vale para
los seis y la cascada pasa al siguiente en cuanto uno se cae, **con la misma
conversación**: lo que ya se consultó sigue ahí y no se repite.

**La tabla salió de probarlos, no de leer sus webs.** Nueve claves, y lo que se
mide no es que la clave valga sino que el modelo llame a herramientas, que es lo
único que necesita este agente y no aparece en ninguna documentación:

| | modelo verificado | |
|---|---|---|
| **groq** | `openai/gpt-oss-120b` | ✅ |
| **google** | `gemini-2.5-flash` | ✅ |
| **mistral** | `mistral-medium-latest` | ✅ `large` da timeout con esta cuenta |
| **cohere** | `command-a-03-2025` | ✅ |
| **openrouter** | `openai/gpt-oss-120b` | ✅ los `:free` dan 404 |
| **nvidia** | `deepseek-ai/deepseek-v4-flash-0731` | ✅ `llama-3.3-70b` está retirado (410) |
| cerebras | — | ❌ 402, saldo 0 |
| sambanova | — | ❌ 402, saldo 0 |
| ai21 | — | ❌ 410, API retirada |

Los tres que no sirven se quedan en el código con el motivo escrito: lo que se
probó y falló es tan útil como lo que funciona, y sin eso alguien vuelve a
intentarlo dentro de tres meses. Fuera de la cascada, eso sí, porque probarlos
solo añade una espera antes de un 402 seguro.

```bash
python agente.py --probar      # vuelve a medir cuál sirve, en 20 segundos
python agente.py --comprobar   # rompe el primero a propósito y ve si la cascada aguanta
PROVEEDOR=google python agente.py   # empieza por uno, los demás quedan de red
```

**Y el que responde se dice.** Si la respuesta la escribió el tercero de la
cascada, va al pie: *«lo respondió mistral; groq (modelo no servido), google
(InternalServerError) no estaban disponibles»*. No es depuración —ocultarlo
sería vender una continuidad que no hubo.

Esa frase es de una ejecución real. Groq lo rompí yo para probar; **Google se
cayó solo**, que es exactamente el caso para el que existe esto.

Las claves se sacan gratis en las consolas de cada uno y van en `.env`, que está
en el `.gitignore`. Una clave publicada la revocan, con razón.

**El catálogo publicado no es el catálogo de tu cuenta.** La documentación de
Groq anuncia `llama-3.3-70b-versatile` y esta clave responde 404 al pedirlo; los
modelos `:free` de OpenRouter anuncian en el propio nombre una cuota que la
cuenta no tiene. Antes de fijar un modelo, pregúntale a la API cuáles sirve:

```bash
python agente.py --modelos
```

## Correrlo

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

python fichas.py          # baja las fichas (una vez)
python fichas.py --indexar  # calcula los vectores
python herramientas.py    # comprueba las herramientas y GBIF, sin gastar cuota
python indice.py --calibrar # vuelve a medir el corte de parecido
python memoria.py         # comprueba que no duplica ni borra de más
python agente.py          # conversación por consola
python agente.py --usuario=diego   # ...que además te recuerda
uvicorn servidor:app --reload
```

En `http://127.0.0.1:8000/docs` está la API:

| Endpoint | Qué es |
|---|---|
| `POST /preguntar` | una pregunta; devuelve la respuesta **y qué herramientas usó** |
| `POST /identificar` | sube una foto y pregunta por ella en la misma llamada |
| `GET /salud` | si está vivo, con qué modelo y qué herramientas tiene |
| `DELETE /conversacion/{id}` | olvida una conversación |
| `GET /memoria/{usuario}` | **qué recuerda de ti**, con tus palabras |
| `DELETE /memoria/{usuario}` | borra tu memoria entera |

Con Docker:

```bash
docker build -t yachaq .
docker run -p 8000:8000 --env-file .env -v yachaq-datos:/datos yachaq
```

El modelo de Riksi, las fichas y sus vectores van **dentro de la imagen**: un
contenedor que depende de una ruta del disco de quien lo construyó no es
portátil. La memoria va **en un volumen**, porque dentro se borraría al recrear
el contenedor, que es justo lo contrario de para lo que existe.

## La memoria: qué se guarda y qué no

Son dos cosas distintas y mezclarlas es el error fácil:

- **La conversación** es el ir y venir de una sesión. Se guarda entera para que
  «¿y dónde vive?» sepa de qué animal se habla.
- **Los recuerdos** son hechos sueltos sobre quien pregunta —qué le interesa,
  por dónde sale al campo— que valen en *cualquier* conversación futura.

Guardar la conversación entera como memoria a largo plazo no funciona: crece sin
tope y llena el contexto de charla. Los recuerdos los extrae el propio modelo
con la herramienta `recordar`, cuando oye algo que le sobrevivirá a la sesión.
Funciona de verdad: en una conversación le dices que te interesan los colibríes
y que sales por el Cajas; en otra, empezando de cero, le preguntas qué buscar el
fin de semana y te propone el colibrí tirio en Azuay.

**El servidor se quedó sin estado.** Las conversaciones vivían en un diccionario
del módulo, y eso eran dos problemas que son el mismo: se perdían al reiniciar y
no se compartían entre workers, así que la segunda pregunta podía caer en otro
proceso que no sabía de qué se hablaba. Ahora cada petición reconstruye el
agente desde la base y lo suelta; abrir un SQLite cuesta menos que un viaje a
Groq, y a cambio se puede replicar y reiniciar sin que nadie lo note.

**Postgres tampoco aquí, y esta vez rompiendo el plan.** Lo prometía para esta
fase. SQLite está en la biblioteca estándar, escribe con WAL y aguanta de sobra
un servidor de un proceso. Postgres gana el día que haya varias réplicas
escribiendo, porque SQLite no comparte fichero entre máquinas. Ese día se cambia
un módulo.

**Lo mismo dicho de otra forma no ocupa dos sitios.** «Me gustan los colibríes»
y «me interesan mucho los colibríes» son el mismo recuerdo; sin comprobarlo, a
las veinte conversaciones el prompt es la misma frase repetida. Se compara por
significado con el codificador que ya usa el RAG y por encima de 0,85 no entra.
Olvidar va por el mismo camino: «olvida lo del Cajas» busca el recuerdo más
parecido y devuelve cuál borró, para que se pueda avisar si no era ese.

**Puedes ver y borrar lo que sabe de ti.** `GET /memoria/{usuario}` lo enseña
con tus palabras y `DELETE` lo tira. Una memoria que no se puede leer ni borrar
es un problema, no una función.

**Quién eres no es un argumento de las herramientas.** Si lo fuera, el modelo
podría escribir otro nombre en la llamada y leer la memoria de alguien más. Va
por fuera, lo fija el servidor. Lo que todavía no hay es autenticación: quien
acierte un nombre lee esa memoria, y para publicarlo eso tiene que ser un token.

## Dos veces que el agente se inventó la geografía

Este proyecto va de distinguir lo consultado de lo recordado, y los dos fallos
más caros fueron del mismo tipo.

Preguntado qué buscar el fin de semana, respondió: *«se ha registrado en
**Pichincha** (donde está Cajas)»*. **El Cajas está en Azuay.** Nadie le había
dicho la provincia: cruzó de memoria el nombre del parque con la provincia que
más salía en los registros, y lo escribió con el mismo tono que el dato real.

El arreglo no fue prohibirlo en el prompt, fue darle cómo consultarlo:
`donde_se_ha_visto` acepta ahora un `lugar`, y GBIF filtra por él. El agente ya
no deduce dónde queda el Cajas; pregunta qué se ha visto allí. Ahora responde
Azuay, y el caso está en la comprobación.

El segundo: con una pregunta abierta se puso a consultar especies una por una
—llegó a buscar *Puya raimondii*, que no está en el catálogo ni vive en el
Ecuador— y agotó las vueltas sin llegar a responder. Le faltaba saber de qué
puede hablar, así que ahora hay una herramienta que devuelve el catálogo. Pasó
de siete llamadas sin respuesta a cuatro con respuesta.

Y por si vuelve a pasar, agotar las vueltas ya no devuelve una rendición vacía:
se pide una última respuesta **sin herramientas** para que diga lo que averiguó
y qué le quedó por mirar. Media respuesta con datos reales vale más que ninguna.

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
- [x] **4 · Memoria** que sobrevive al reinicio: qué te interesa, por dónde
      sales al campo. En SQLite, y el servidor se quedó sin estado.
- [ ] **5 · Servidor MCP** para usar estas herramientas desde Claude Code o
      cualquier otro cliente.
- [ ] **6 · Multi-agente**: uno identifica, otro verifica contra los registros,
      un coordinador decide.

## Licencia

Código bajo MIT. El modelo de identificación viene de Riksi y se publica bajo
CC-BY 4.0; los datos de observaciones son de [GBIF](https://www.gbif.org).
