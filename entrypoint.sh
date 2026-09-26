#!/bin/sh

# Esperar a que la base de datos esté lista si es necesario
# (Opcional, pero recomendado si usas una DB externa)
echo "Esperando a que la base de datos se inicie..."
sleep 5

# Ejecutar migraciones
echo "Ejecutando migraciones de base de datos..."
python manage.py migrate --noinput

# Ejecutar el comando original
echo "Iniciando el servidor de Django..."
exec "$@"
