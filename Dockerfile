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
ENV RIKSI_MODELO=/app/modelo

EXPOSE 8000
CMD ["uvicorn", "servidor:app", "--host", "0.0.0.0", "--port", "8000"]
