"""
sync_comprobantes.py
--------------------
Descarga los comprobantes recibidos de ARCA (via Afip SDK)
y los sincroniza en un Google Sheet (una pestaña por cliente/sociedad).

Variables de entorno requeridas (GitHub Secrets):
  - AFIPSDK_TOKEN         : access token de Afip SDK
  - GOOGLE_CREDENTIALS    : contenido del JSON de la service account de Google
  - SPREADSHEET_ID        : ID del Google Sheet destino
  - CLIENTS_JSON          : JSON con los datos de clientes (ver abajo)
  - DAYS_BACK             : (opcional) días hacia atrás a consultar. Default: 3
                            Usar 31 para carga inicial del mes completo.

Formato de CLIENTS_JSON:
[
  {
    "nombre": "Salomone Martin",
    "username": "20310889475",
    "password": "clave123",
    "entities": [
      {"cuit": "20310889475", "nombre": "Salomone Martin"},
      {"cuit": "30718439406", "nombre": "Alcortapipol SRL"},
      {"cuit": "30715288164", "nombre": "Florasalvaje SRL"}
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

COLUMNS = [
    "Fecha de Emisión",
    "Tipo de Comprobante",
    "Punto de Venta",
    "Número Desde",
    "Número Hasta",
    "Cód. Autorización",
    # Emisor
    "Tipo Doc. Emisor",
    "Nro. Doc. Emisor",
    "Denominación Emisor",
    # Receptor
    "Tipo Doc. Receptor",
    "Nro. Doc. Receptor",
    # Moneda
    "Moneda",
    "Tipo Cambio",
    # Importes por alícuota
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
    # Totales
    "Imp. Neto Gravado Total",
    "Imp. Neto No Gravado",
    "Imp. Op. Exentas",
    "Otros Tributos",
    "Total IVA",
    "Imp. Total",
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_amount(value) -> float:
    """Convierte '3.117.355,37' o '654644,63' a float. Vacío → 0."""
    s = str(value).strip()
    if s in ("", "-", "0"):
        return 0.0
    cleaned = s.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def format_row(comp: dict) -> list:
    row = []
    for col in COLUMNS:
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

def create_automation(token: str, cuit: str, username: str, password: str,
                      fecha_desde_str: str, fecha_hasta_str: str) -> str:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "automation": "mis-comprobantes",
        "params": {
            "cuit": cuit,
            "username": username,
            "password": password,
            "filters": {
                "t": "R",
                "fechaEmision": f"{fecha_desde_str} - {fecha_hasta_str}",
            },
        },
    }
    resp = requests.post(f"{AFIPSDK_BASE}/automations", json=payload,
                         headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def poll_automation(token: str, automation_id: str) -> list[dict]:
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
    raise TimeoutError(f"Timeout esperando automatización {automation_id}")


def fetch_with_retry(token: str, cuit: str, username: str, password: str,
                     desde_str: str, hasta_str: str) -> list[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  → Consultando {desde_str} - {hasta_str} (intento {attempt})")
            aid = create_automation(token, cuit, username, password, desde_str, hasta_str)
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


def fetch_chunked(token: str, cuit: str, username: str, password: str,
                  fecha_desde: date, fecha_hasta: date) -> list[dict]:
    all_comp = []
    for chunk_start, chunk_end in date_chunks(fecha_desde, fecha_hasta, CHUNK_DAYS):
        all_comp.extend(fetch_with_retry(
            token, cuit, username, password,
            chunk_start.strftime("%d/%m/%Y"),
            chunk_end.strftime("%d/%m/%Y")
        ))
    return all_comp


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_or_create_worksheet(spreadsheet, sheet_name: str):
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=5000, cols=len(COLUMNS) + 2)
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
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        return set()
    try:
        cae_col = all_values[0].index("Cód. Autorización")
        return {row[cae_col] for row in all_values[1:]
                if len(row) > cae_col and row[cae_col]}
    except ValueError:
        return set()


def append_comprobantes(worksheet, comprobantes: list[dict], existing_caes: set) -> int:
    rows_to_add = []
    for comp in comprobantes:
        cae = comp.get("Cód. Autorización", "")
        if cae and cae in existing_caes:
            continue
        rows_to_add.append(format_row(comp))
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

    total_nuevos = 0

    for client in clients:
        username = client["username"]
        password = client["password"]
        entities = client.get("entities", [
            {"cuit": client.get("cuit", username), "nombre": client["nombre"]}
        ])

        for entity in entities:
            cuit       = entity["cuit"]
            sheet_name = f"{entity['nombre']} - Recibidos"

            print(f"━━━ {entity['nombre']} ({cuit}) ━━━")

            try:
                comprobantes = fetch_chunked(
                    afipsdk_token, cuit, username, password,
                    fecha_desde, fecha_hasta
                )

                if not comprobantes:
                    print(f"  → Sin comprobantes en el período\n")
                    continue

                worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
                write_header_if_needed(worksheet)
                apply_currency_format(spreadsheet, worksheet)
                existing_caes = get_existing_caes(worksheet)
                nuevos = append_comprobantes(worksheet, comprobantes, existing_caes)
                total_nuevos += nuevos
                print(f"  → {nuevos} comprobante(s) nuevo(s)\n")

            except Exception as e:
                print(f"  ❌ Error: {e}\n")

    print(f"✅ Listo. Total nuevos: {total_nuevos}")


if __name__ == "__main__":
    main()
