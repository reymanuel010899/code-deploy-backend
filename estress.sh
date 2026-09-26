#!/bin/bash

ENDPOINT="http://cluste-ecsal-rkik9jhgv4kt-1048153987.us-east-1.elb.amazonaws.com/"
REQUESTS_PER_SECOND=10000     # Número de peticiones por segundo
DURATION_SECONDS=10        # Duración total de la prueba

OK_COUNT=0
FAIL_COUNT=0

echo "Iniciando stress test a $ENDPOINT con $REQUESTS_PER_SECOND peticiones/seg durante $DURATION_SECONDS segundos..."

for ((i=1; i<=DURATION_SECONDS; i++))
do
  for ((j=1; j<=REQUESTS_PER_SECOND; j++))
  do
    (
      STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$ENDPOINT")
      if [ "$STATUS_CODE" -eq 200 ]; then
        ((OK_COUNT++))
      else
        ((FAIL_COUNT++))
      fi
    ) &
  done
  sleep 1
done

wait

TOTAL=$((OK_COUNT + FAIL_COUNT))

echo "-----------------------------------------"
echo "✅ Total peticiones:     $TOTAL"
echo "✅ Respuestas 200 (OK):  $OK_COUNT"
echo "❌ Fallos (no 200):      $FAIL_COUNT"
echo "-----------------------------------------"