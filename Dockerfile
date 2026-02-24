# Utiliza una imagen oficial de Python como base
FROM python:3.11-slim

# Establece el directorio de trabajo
WORKDIR /app

# Copia los archivos de requerimientos y los instala
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código del proyecto
COPY . .

# Expone el puerto por defecto de Django
EXPOSE 8000

# Comando por defecto para correr el servidor
CMD ["sh", "-c", "sleep 10 && python manage.py runserver 0.0.0.0:8000"]