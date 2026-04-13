"""
sync_comprobantes.py
--------------------
Descarga comprobantes recibidos Y emitidos de ARCA (via Afip SDK)
y los sincroniza en un Google Sheet con:
  - "Emitidos"   : todos los clientes en una sola pestaña
  - "Recibidos"  : todos los clientes en una sola pestaña

El resumen de IVA se calcula con fórmulas directamente en el Sheet.

Variables de entorno requeridas (GitHub Secrets):
  - AFIPSDK_TOKEN      : access token de Afip SDK
  - GOOGLE_CREDENTIALS : contenido del JSON de la service account de Google
  - SPREADSHEET_ID     : ID del Google Sheet destino
  - CLIENTS_JSON       : JSON con los datos de clientes

Formato de CLIENTS_JSON:
[
  {
    "nombre": "Alcortapipol SRL",
    "username": "20310889475",
    "password": "clave123",
    "entities": [
      {"cuit": "30718439406", "nombre": "Alcortapipol SRL"}
    ]
  },
  {
    "nombre": "Obrador Florida",
    "username": "23234071344",
    "password": "clave456"
  }
]
"""

import os
import json
import time
import requests
from datetime import date, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ─── Constantes ───────────────────────────────────────────────────────────────

AFIPSDK_BASE  = "https://app.afipsdk.com/api/v1"
POLL_INTERVAL = 5
MAX_WAIT      = 300
MAX_RETRIES   = 3
RETRY_WAIT    = 15
CHUNK_DAYS    = 15

CURRENCY_FIELDS = {
    "Imp. Neto Gravado IVA 0%",
    "Imp. Neto Gravado IVA 2,5%",
    "Imp. Neto Gravado IVA 5%",
    "Imp. Neto Gravado IVA 10,5%",
    "Imp. Neto Gravado IVA 21%",
    "Imp. Neto Gravado IVA 27%",
    "Imp. Neto Gravado Total",
    "IVA 2,5%",
    "IVA 5%",
    "IVA 10,5%",
    "IVA 21%",
    "IVA 27%",
    "Total IVA",
    "Imp. Neto No Gravado",
    "Imp. Op. Exentas",
    "Otros Tributos",
    "Imp. Total",
}

# Razón Social va primero para identificar el cliente
COLUMNS = [
    "Razón Social",
    "Fecha de Emisión",
    "Tipo de Comprobante",
    "Punto de Venta",
    "Número Desde",
    "Número Hasta",
    "Cód. Autorización",
    "Tipo Doc. Emisor",
    "Nro. Doc. Emisor",
    "Denominación Emisor",
    "Tipo Doc. Receptor",
    "Nro. Doc. Receptor",
    "Moneda",
    "Tipo Cambio",
    "Imp. Neto Gravado IVA 0%",
    "Imp. Neto Gravado IVA 2,5%",
    "IVA 2,5%",
    "Imp. Neto Gravado IVA 5%",
    "IVA 5%",
    "Imp. Neto Gravado IVA 10,5%",
    "IVA 10,5%",
    "Imp. Neto Gravado IVA 21%",
    "IVA 21%",
    "Imp. Neto Gravado IVA 27%",
    "IVA 27%",
    "Imp. Neto Gravado Total",
    "Imp. Neto No Gravado",
    "Imp. Op. Exentas",
    "Otros Tributos",
    "Total IVA",
    "Imp. Total",
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_amount(value) -> float:
    s = str(value).strip()
    if s in ("", "-", "0"):
        return 0.0
    cleaned = s.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def format_row(comp: dict, razon_social: str) -> list:
    row = [razon_social]  # primera columna siempre
    for col in COLUMNS[1:]:  # saltamos "Razón Social" que ya agregamos
        val = comp.get(col, "")
        if col in CURRENCY_FIELDS:
            row.append(parse_amount(val))
        else:
            row.append(val if val is not None else "")
    return row


def date_chunks(fecha_desde: date, fecha_hasta: date, chunk_days: int):
    current = fecha_desde
    while current <= fecha_hasta:
        end = min(current + timedelta(days=chunk_days - 1), fecha_hasta)
        yield current, end
        current = end + timedelta(days=1)


# ─── Afip SDK ─────────────────────────────────────────────────────────────────

def create_automation(token, cuit, username, password, desde_str, hasta_str, tipo):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "automation": "mis-comprobantes",
        "params": {
            "cuit": cuit,
            "username": username,
            "password": password,
            "filters": {
                "t": tipo,
                "fechaEmision": f"{desde_str} - {hasta_str}",
            },
        },
    }
    resp = requests.post(f"{AFIPSDK_BASE}/automations", json=payload,
                         headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def poll_automation(token, automation_id):
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0
    while elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        resp = requests.get(f"{AFIPSDK_BASE}/automations/{automation_id}",
                            headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "complete":
            return data.get("data", [])
        elif status == "error":
            msg = data.get("data", {}).get("message", "Error desconocido")
            raise RuntimeError(f"Error ARCA: {msg}")
    raise TimeoutError(f"Timeout esperando {automation_id}")


def fetch_with_retry(token, cuit, username, password, desde_str, hasta_str, tipo):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  → {tipo} | {desde_str} - {hasta_str} (intento {attempt})")
            aid = create_automation(token, cuit, username, password,
                                    desde_str, hasta_str, tipo)
            result = poll_automation(token, aid)
            print(f"    ✓ {len(result)} comprobante(s)")
            return result
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 500:
                print(f"    ⚠ Error 500 (intento {attempt}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_WAIT)
                else:
                    raise RuntimeError(f"Falló tras {MAX_RETRIES} intentos: {e}")
            else:
                raise
    return []


def fetch_chunked(token, cuit, username, password, fecha_desde, fecha_hasta, tipo):
    all_comp = []
    for chunk_start, chunk_end in date_chunks(fecha_desde, fecha_hasta, CHUNK_DAYS):
        all_comp.extend(fetch_with_retry(
            token, cuit, username, password,
            chunk_start.strftime("%d/%m/%Y"),
            chunk_end.strftime("%d/%m/%Y"),
            tipo
        ))
    return all_comp


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_or_create_worksheet(spreadsheet, sheet_name):
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=10000, cols=len(COLUMNS) + 2)
        print(f"  → Pestaña '{sheet_name}' creada")
        return ws


def write_header_if_needed(worksheet):
    if not worksheet.row_values(1):
        worksheet.append_row(COLUMNS, value_input_option="RAW")
        print("  → Cabecera escrita")


def apply_currency_format(spreadsheet, worksheet):
    try:
        col_indices = [i for i, c in enumerate(COLUMNS) if c in CURRENCY_FIELDS]
        reqs = [{
            "repeatCell": {
                "range": {
                    "sheetId": worksheet.id,
                    "startRowIndex": 1,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        } for col_idx in col_indices]
        if reqs:
            spreadsheet.batch_update({"requests": reqs})
    except Exception as e:
        print(f"  ⚠ Formato moneda: {e}")


def get_existing_caes(worksheet) -> set:
    """Devuelve set de CAE+RazonSocial para evitar duplicados por cliente."""
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        return set()
    try:
        header = all_values[0]
        cae_col = header.index("Cód. Autorización")
        rs_col  = header.index("Razón Social")
        return {
            f"{row[cae_col]}|{row[rs_col]}"
            for row in all_values[1:]
            if len(row) > cae_col and row[cae_col]
        }
    except ValueError:
        return set()


def append_comprobantes(worksheet, comprobantes, razon_social, existing_keys) -> int:
    rows_to_add = []
    for comp in comprobantes:
        cae = comp.get("Cód. Autorización", "")
        key = f"{cae}|{razon_social}"
        if cae and key in existing_keys:
            continue
        rows_to_add.append(format_row(comp, razon_social))
    if rows_to_add:
        worksheet.append_rows(rows_to_add, value_input_option="USER_ENTERED")
    return len(rows_to_add)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    days_back   = int(os.environ.get("DAYS_BACK", "3"))
    fecha_hasta = date.today() - timedelta(days=1)
    fecha_desde = fecha_hasta - timedelta(days=days_back - 1)

    print(f"📅 Período: {fecha_desde.strftime('%d/%m/%Y')} → {fecha_hasta.strftime('%d/%m/%Y')}")
    print(f"   ({days_back} días, chunks de {CHUNK_DAYS})\n")

    afipsdk_token    = os.environ["AFIPSDK_TOKEN"]
    spreadsheet_id   = os.environ["SPREADSHEET_ID"]
    credentials_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    clients          = json.loads(os.environ["CLIENTS_JSON"])

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds       = Credentials.from_service_account_info(credentials_json, scopes=scopes)
    gc          = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_id)

    # Obtener/crear las dos pestañas globales
    ws_rec = get_or_create_worksheet(spreadsheet, "Recibidos")
    ws_emi = get_or_create_worksheet(spreadsheet, "Emitidos")
    write_header_if_needed(ws_rec)
    write_header_if_needed(ws_emi)
    time.sleep(2)
    apply_currency_format(spreadsheet, ws_rec)
    apply_currency_format(spreadsheet, ws_emi)

    total_nuevos = 0

    for client in clients:
        username = client["username"]
        password = client["password"]
        entities = client.get("entities", [
            {"cuit": client.get("cuit", username), "nombre": client["nombre"]}
        ])

        for entity in entities:
            cuit         = entity["cuit"]
            razon_social = entity["nombre"]

            print(f"━━━ {razon_social} ({cuit}) ━━━")

            try:
                # Leer CAEs existentes por cliente (frescos en cada iteracion)
                time.sleep(2)
                existing_rec = get_existing_caes(ws_rec)
                time.sleep(2)
                existing_emi = get_existing_caes(ws_emi)

                # Recibidos
                print(f"  [Recibidos]")
                recibidos = fetch_chunked(
                    afipsdk_token, cuit, username, password,
                    fecha_desde, fecha_hasta, "R"
                )
                nuevos_rec = append_comprobantes(ws_rec, recibidos, razon_social, existing_rec)
                total_nuevos += nuevos_rec
                print(f"  → {nuevos_rec} recibido(s) nuevo(s)")

                time.sleep(2)

                # Emitidos
                print(f"  [Emitidos]")
                emitidos = fetch_chunked(
                    afipsdk_token, cuit, username, password,
                    fecha_desde, fecha_hasta, "E"
                )
                nuevos_emi = append_comprobantes(ws_emi, emitidos, razon_social, existing_emi)
                total_nuevos += nuevos_emi
                print(f"  → {nuevos_emi} emitido(s) nuevo(s)")

            except Exception as e:
                print(f"  ❌ Error: {e}")

            print()

    print(f"✅ Listo. Total nuevos: {total_nuevos}")


if __name__ == "__main__":
    main()
