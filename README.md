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

**El esquema que ve el modelo se genera de las firmas de las funciones**, no se
escribe aparte. Un esquema escrito a mano es una segunda fuente de verdad que
se desincroniza en cuanto alguien añade un argumento, y el fallo aparece como
un modelo que llama mal a la herramienta, no como un error.

## El proveedor es intercambiable

Groq y Cerebras regalan cuota y ambos hablan el mismo dialecto que OpenAI, así
que el mismo código sirve para los dos. Se cambia con una variable de entorno:

```bash
PROVEEDOR=groq      # llama-3.3-70b-versatile, con herramientas en paralelo
PROVEEDOR=cerebras  # gpt-oss-120b
```

Sacar la clave es gratis en [console.groq.com/keys](https://console.groq.com/keys)
o [cloud.cerebras.ai](https://cloud.cerebras.ai). Va en `clave.txt` o en la
variable `GROQ_API_KEY`; el fichero está en el `.gitignore`.

## Correrlo

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt

python herramientas.py    # comprueba las herramientas y GBIF, sin gastar cuota
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

**El agente hereda el «no lo sé» de Riksi.** El modelo de fotos trae su umbral
calibrado, y cuando la confianza no llega, el agente lo dice en vez de afirmar.
En el campo, una identificación equivocada dada con seguridad hace más daño que
no dar ninguna.

## Plan

- [x] **1 · API**. FastAPI, Docker, endpoint de conversación.
- [x] **2 · Herramientas**. El modelo decide cuándo identificar, buscar o
      consultar registros, y puede pedir varias cosas a la vez.
- [ ] **3 · RAG** sobre fichas de especies, con Postgres y pgvector. Los
      embeddings van en local con `sentence-transformers`: ni Groq ni Cerebras
      los ofrecen, y así el proyecto no gana una dependencia de pago.
- [ ] **4 · Memoria** que sobreviva al reinicio: qué especies te interesan,
      dónde sales al campo.
- [ ] **5 · Servidor MCP** para usar estas herramientas desde Claude Code o
      cualquier otro cliente.
- [ ] **6 · Multi-agente**: uno identifica, otro verifica contra los registros,
      un coordinador decide.

## Licencia

Código bajo MIT. El modelo de identificación viene de Riksi y se publica bajo
CC-BY 4.0; los datos de observaciones son de [GBIF](https://www.gbif.org).
