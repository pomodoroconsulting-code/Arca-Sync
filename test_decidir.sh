#!/usr/bin/env bash
# Prueba el job `decidir` de daily_sync.yml sin tocar GitHub.
#
# Saca el bloque `run` del YAML tal cual se va a ejecutar, le pone un `gh` falso
# que contesta lo que le digamos, y chequea las dos salidas que importan:
# `correr` (si se gasta cuota de AFIP SDK) y `solo_cuits` (si se reintenta todo
# o solo los que fallaron).
set -u
cd "$(dirname "$0")"
YML=.github/workflows/daily_sync.yml
BLOQUE=$(python3 -c "
import yaml
print(yaml.safe_load(open('$YML'))['jobs']['decidir']['steps'][0]['run'], end='')
")

HOY=$(TZ=America/Argentina/Buenos_Aires date +%F)
AYER=$(TZ=America/Argentina/Buenos_Aires date -v-1d +%F 2>/dev/null \
  || TZ=America/Argentina/Buenos_Aires date -d yesterday +%F)

fallos=0

# $1 nombre  $2 evento  $3 json de `gh run list`  $4 hay artifact  $5 correr esperado  $6 solo_cuits esperado
caso() {
  local nombre=$1 evento=$2 runs=$3 artifact=$4 esp_correr=$5 esp_cuits=$6
  local tmp; tmp=$(mktemp -d)

  # `gh` falso: run list devuelve el json del caso, run download crea (o no) el
  # artifact con los CUIT fallidos.
  cat > "$tmp/gh" <<GH
#!/usr/bin/env bash
if [ "\$2" = "list" ]; then
  echo '$runs' | jq -r "\$(for a in "\$@"; do [ "\$prev" = "--jq" ] && echo "\$a"; prev=\$a; done)"
  exit 0
fi
if [ "\$2" = "download" ]; then
  [ "$artifact" = "si" ] || exit 1
  d=""; prev=""
  for a in "\$@"; do [ "\$prev" = "-D" ] && d=\$a; prev=\$a; done
  mkdir -p "\$d" && echo '["20-11111111-1","20-22222222-2"]' > "\$d/fallidos.json"
  exit 0
fi
exit 1
GH
  chmod +x "$tmp/gh"

  : > "$tmp/out"
  # El YAML trae las expresiones ${{ ... }} de Actions; se reemplazan por los
  # valores que tendría el runner en este caso.
  printf '%s' "$BLOQUE" \
    | sed -e "s|\${{ github.event_name }}|$evento|g" \
          -e "s|\${{ github.event.schedule }}|0 11 * * *|g" \
          -e "s|\${{ github.repository }}|org/repo|g" \
          -e 's|-R "\$GITHUB_REPOSITORY"|-R org/repo|g' \
    > "$tmp/bloque.sh"

  ( cd "$tmp" && PATH="$tmp:$PATH" GITHUB_OUTPUT="$tmp/out" GITHUB_REPOSITORY=org/repo \
      bash bloque.sh >"$tmp/log" 2>&1 )

  local correr cuits
  correr=$(grep '^correr=' "$tmp/out" | tail -1 | cut -d= -f2-)
  cuits=$(grep '^solo_cuits=' "$tmp/out" | tail -1 | cut -d= -f2-)

  if [ "$correr" = "$esp_correr" ] && [ "$cuits" = "$esp_cuits" ]; then
    printf 'ok    %s\n' "$nombre"
  else
    printf 'FALLA %s\n      esperaba correr=%s solo_cuits=%s\n      obtuvo   correr=%s solo_cuits=%s\n' \
      "$nombre" "$esp_correr" "$esp_cuits" "$correr" "$cuits"
    sed 's/^/      | /' "$tmp/log"
    fallos=$((fallos + 1))
  fi
  rm -rf "$tmp"
}

ok_hoy="[{\"databaseId\":111,\"status\":\"completed\",\"conclusion\":\"success\",\"createdAt\":\"${HOY}T13:00:49Z\"}]"
mal_hoy="[{\"databaseId\":222,\"status\":\"completed\",\"conclusion\":\"failure\",\"createdAt\":\"${HOY}T13:00:49Z\"}]"
cancel_hoy="[{\"databaseId\":333,\"status\":\"completed\",\"conclusion\":\"cancelled\",\"createdAt\":\"${HOY}T13:00:49Z\"}]"
ok_ayer="[{\"databaseId\":444,\"status\":\"completed\",\"conclusion\":\"success\",\"createdAt\":\"${AYER}T13:00:49Z\"}]"
# Una corrida tarde: 23:30 ART de ayer es 02:30 UTC de hoy. Contando en UTC
# parecería de hoy; en hora de Argentina es de ayer.
tarde_ayer="[{\"databaseId\":555,\"status\":\"completed\",\"conclusion\":\"success\",\"createdAt\":\"${HOY}T02:30:00Z\"}]"
nada="[]"
CUITS="20-11111111-1,20-22222222-2"

echo "--- el turno agendado de GitHub ---"
caso "Vercel ya corrió bien hoy → no corre"            schedule "$ok_hoy"     no  false ""
caso "hoy falló → reintenta solo los que fallaron"      schedule "$mal_hoy"    si  true  "$CUITS"
caso "hoy falló sin lista → corre completo"             schedule "$mal_hoy"    no  true  ""
caso "hoy la cancelaron → corre completo"               schedule "$cancel_hoy" no  true  ""
caso "hoy no corrió nadie → corre completo (la red)"    schedule "$ok_ayer"    no  true  ""
caso "corrida de anoche 23:30 ART → sigue siendo ayer"  schedule "$tarde_ayer" no  true  ""
caso "repo sin corridas previas → corre completo"       schedule "$nada"       no  true  ""

echo "--- el botón manual ---"
caso "manual sobre una corrida exitosa → corre igual"  workflow_dispatch "$ok_hoy" no true ""

echo
[ "$fallos" = 0 ] && echo "todo bien" || { echo "$fallos caso(s) mal"; exit 1; }
