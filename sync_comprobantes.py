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
                            Usar 31 para la carga inicial del mes completo.

Formato de CLIENTS_JSON:
[
  {
    "nombre": "Juan Pérez",
    "username": "20111111112",
    "password": "clave123",
    "entities": [
      {"cuit": "20111111112", "nombre": "Juan Pérez"},
      {"cuit": "30711111113", "nombre": "MI EMPRESA SRL"}
    ]
  },
  {
    "nombre": "Cliente Simple",
    "username": "27222222223",
    "password": "clave456"
  }
]

Si un cliente no tiene sociedades, omitir "entities" y agregar solo "cuit".
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
POLL_INTERVAL = 5    # segundos entre cada chequeo
MAX_WAIT      = 300  # tiempo máximo de espera por automatización (seg)

CURRENCY_FIELDS = {
    "Imp. Neto Gravado",
    "Imp. Neto No Gravado",
    "Imp. Op. Exentas",
    "Otros Tributos",
    "IVA",
    "Imp. Total",
}

COLUMNS = [
    "Fecha de Emisión",
    "Tipo de Comprobante",
    "Punto de Venta",
    "Número Desde",
    "Número Hasta",
    "Cód. Autorización",
    "Tipo Doc. Receptor",
    "Nro. Doc. Receptor",
    "Denominación Receptor",
    "Moneda",
    "Tipo Cambio",
    "Imp. Neto Gravado",
    "Imp. Neto No Gravado",
    "Imp. Op. Exentas",
    "Otros Tributos",
    "IVA",
    "Imp. Total",
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_amount(value: str) -> float:
    """Convierte '3.772.000,00' o '40136,00' a float."""
    if not value or str(value).strip() == "":
        return 0.0
    cleaned = str(value).replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def format_row(comp: dict) -> list:
    """Construye la fila para el Sheet convirtiendo montos a float."""
    row = []
    for col in COLUMNS:
        val = comp.get(col, "")
        if col in CURRENCY_FIELDS:
            row.append(parse_amount(str(val)))
        else:
            row.append(val)
    return row


# ─── Afip SDK ─────────────────────────────────────────────────────────────────

def create_automation(token: str, cuit: str, username: str, password: str,
                      fecha_desde: str, fecha_hasta: str) -> str:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "automation": "mis-comprobantes",
        "params": {
            "cuit": cuit,
            "username": username,
            "password": password,
            "filters": {
                "t": "R",
                "fechaEmision": f"{fecha_desde} - {fecha_hasta}",
            },
        },
    }
    resp = requests.post(f"{AFIPSDK_BASE}/automations", json=payload,
                         headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(f"  → Automatización creada: {data['id']} (status: {data['status']})")
    return data["id"]


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
            comprobantes = data.get("data", [])
            print(f"  → Completado: {len(comprobantes)} comprobante(s)")
            return comprobantes
        elif status == "error":
            msg = data.get("data", {}).get("message", "Error desconocido")
            raise RuntimeError(f"Error en automatización: {msg}")
        else:
            print(f"  → Esperando... ({elapsed}s)")
    raise TimeoutError(f"La automatización {automation_id} no terminó en {MAX_WAIT}s")


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
    """Aplica formato #,##0.00 a las columnas de moneda."""
    try:
        col_indices = [COLUMNS.index(c) for c in COLUMNS if c in CURRENCY_FIELDS]
        reqs = []
        for col_idx in col_indices:
            reqs.append({
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
            })
        if reqs:
            spreadsheet.batch_update({"requests": reqs})
    except Exception as e:
        print(f"  ⚠ No se pudo aplicar formato moneda: {e}")


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
    days_back    = int(os.environ.get("DAYS_BACK", "3"))
    fecha_hasta  = date.today() - timedelta(days=1)
    fecha_desde  = fecha_hasta - timedelta(days=days_back - 1)
    fecha_desde_str = fecha_desde.strftime("%d/%m/%Y")
    fecha_hasta_str = fecha_hasta.strftime("%d/%m/%Y")

    print(f"📅 Período: {fecha_desde_str} → {fecha_hasta_str}\n")

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

        # Armar lista de entidades a consultar
        if "entities" in client:
            entities = client["entities"]
        else:
            entities = [{"cuit": client.get("cuit", username), "nombre": client["nombre"]}]

        for entity in entities:
            cuit       = entity["cuit"]
            sheet_name = f"{entity['nombre']} - Recibidos"

            print(f"━━━ {entity['nombre']} ({cuit}) ━━━")

            try:
                automation_id = create_automation(
                    afipsdk_token, cuit, username, password,
                    fecha_desde_str, fecha_hasta_str
                )
                comprobantes = poll_automation(afipsdk_token, automation_id)

                if not comprobantes:
                    print(f"  → Sin comprobantes en el período\n")
                    continue

                worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
                write_header_if_needed(worksheet)
                apply_currency_format(spreadsheet, worksheet)
                existing_caes = get_existing_caes(worksheet)
                nuevos = append_comprobantes(worksheet, comprobantes, existing_caes)
                total_nuevos += nuevos
                print(f"  → {nuevos} comprobante(s) nuevo(s) agregado(s)\n")

            except Exception as e:
                print(f"  ❌ Error: {e}\n")

    print(f"✅ Listo. Total nuevos: {total_nuevos}")


if __name__ == "__main__":
    main()
