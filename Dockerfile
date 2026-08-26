# El modelo de Riksi son 3,8 MB y va dentro de la imagen: un contenedor que
# depende de una ruta del disco del anfitrión no es portátil.
FROM python:3.12-slim

WORKDIR /app

# Las dependencias primero y en su propia capa: cambian mucho menos que el
# código, así que Docker reutiliza la capa en cada reconstrucción.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# El modelo de embeddings del RAG son 220 MB que se bajan la primera vez que se
# usa. Bajarlos aquí y no en el arranque: si no, el primer usuario del
# contenedor paga la descarga esperando su respuesta.
RUN python -c "from fastembed import TextEmbedding;     TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY *.py ./
COPY modelo/ ./modelo/
# Las fichas y sus vectores van dentro: sin ellos el agente arranca pero
# `consultar_fichas` devuelve vacío siempre, y eso no da error, da un agente que
# dice no saber nada de biología.
COPY fichas.jsonl vectores.npy ./
ENV RIKSI_MODELO=/app/modelo

# La memoria fuera del sistema de ficheros del contenedor. Dentro se borraría al
# recrearlo, que es justo lo contrario de para lo que existe.
ENV YACHAQ_MEMORIA=/datos/memoria.db
VOLUME /datos

EXPOSE 8000
CMD ["uvicorn", "servidor:app", "--host", "0.0.0.0", "--port", "8000"]
