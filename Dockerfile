# El modelo de Riksi son 3,8 MB y va dentro de la imagen: un contenedor que
# depende de una ruta del disco del anfitrión no es portátil.
FROM python:3.12-slim

WORKDIR /app

# Las dependencias primero y en su propia capa: cambian mucho menos que el
# código, así que Docker reutiliza la capa en cada reconstrucción.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY modelo/ ./modelo/
# Las fichas y sus vectores van dentro: sin ellos el agente arranca pero
# `consultar_fichas` devuelve vacío siempre, y eso no da error, da un agente que
# dice no saber nada de biología.
COPY fichas.jsonl vectores.npy ./
ENV RIKSI_MODELO=/app/modelo

# El codificador, en una sola pasada. `--preparar` baja el modelo si no está y
# escribe los pesos fuera del .onnx, que es lo que hace que quepa: cargados por
# fastembed son 671 MB de RAM y con los pesos mapeados desde disco son 388, con
# el mismo resultado (coseno 1,0 contra fastembed).
#
# Va al construir y no al arrancar por dos razones: el primero que pregunte no
# paga la descarga, y si el modelo desapareciera de su origen el fallo saldría
# aquí y no en producción.
#
# Después se borra la caché intermedia: son otros 235 MB de los mismos pesos que
# ya están en codificador-ligero/, y una imagen con todo por duplicado tarda más
# en desplegarse cada vez.
RUN python codificador.py --preparar &&     rm -rf /tmp/fastembed_cache /root/.cache/fastembed

# La memoria fuera del sistema de ficheros del contenedor. Dentro se borraría al
# recrearlo, que es justo lo contrario de para lo que existe.
ENV YACHAQ_MEMORIA=/datos/memoria.db
VOLUME /datos

# El puerto lo pone el proveedor: Koyeb, Render y Fly inyectan $PORT y el
# contenedor tiene que escuchar ahí, no en uno fijo. 8000 es solo el respaldo
# para correrlo en local.
ENV PORT=8000
EXPOSE 8000

# Un worker, no varios: cada uno carga su copia del codificador y del modelo de
# fotos, así que dos workers no dan el doble de capacidad, dan el doble de RAM y
# el contenedor muere. La concurrencia real la da que las herramientas esperan a
# la red, no a la CPU.
CMD ["sh", "-c", "uvicorn servidor:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
