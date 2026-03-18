"""
sync_comprobantes.py
--------------------
Descarga los comprobantes recibidos del día anterior de ARCA (via Afip SDK)
y los sincroniza en un Google Sheet (una pestaña por cliente).

Requiere las siguientes variables de entorno (GitHub Secrets):
  - AFIPSDK_TOKEN         : tu access token de Afip SDK
  - GOOGLE_CREDENTIALS    : contenido del JSON de la service account de Google
  - SPREADSHEET_ID        : ID del Google Sheet destino
  - CLIENTS_JSON          : JSON con los datos de tus clientes (ver README)
"""

import os
import json
import time
import requests
from datetime import date, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ─── Constantes ──────────────────────────────────────────────────────────────

AFIPSDK_BASE = "https://app.afipsdk.com/api/v1"
POLL_INTERVAL = 5        # segundos entre cada chequeo de estado
MAX_WAIT = 180           # tiempo máximo de espera por automatización (seg)

# Columnas que se van a guardar en el Sheet (en este orden)
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

# ─── Afip SDK ─────────────────────────────────────────────────────────────────

def create_automation(token: str, cuit: str, username: str, password: str, fecha: str) -> str:
    """Inicia la automatización y devuelve el ID."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "automation": "mis-comprobantes",
        "params": {
            "cuit": cuit,
            "username": username,
            "password": password,
            "filters": {
                "t": "R",
                "fechaEmision": f"{fecha} - {fecha}",
            },
        },
    }
    resp = requests.post(f"{AFIPSDK_BASE}/automations", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(f"  → Automatización creada: {data['id']} (status: {data['status']})")
    return data["id"]


def poll_automation(token: str, automation_id: str) -> list[dict]:
    """Espera hasta que la automatización termine y devuelve los comprobantes."""
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0
    while elapsed < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        resp = requests.get(f"{AFIPSDK_BASE}/automations/{automation_id}", headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "complete":
            comprobantes = data.get("data", [])
            print(f"  → Completado: {len(comprobantes)} comprobante(s) encontrados")
            return comprobantes
        elif status == "error":
            msg = data.get("data", {}).get("message", "Error desconocido")
            raise RuntimeError(f"Error en automatización: {msg}")
        else:
            print(f"  → Esperando... ({elapsed}s)")
    raise TimeoutError(f"La automatización {automation_id} no terminó en {MAX_WAIT}s")


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_sheet(spreadsheet_id: str, sheet_name: str, credentials_json: dict):
    """Obtiene (o crea) una pestaña en el spreadsheet."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(credentials_json, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(COLUMNS) + 2)
        print(f"  → Pestaña '{sheet_name}' creada")

    return worksheet


def get_existing_caes(worksheet) -> set:
    """Devuelve el conjunto de CAEs ya cargados (para evitar duplicados)."""
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        return set()
    try:
        header = all_values[0]
        cae_col = header.index("Cód. Autorización")
        return {row[cae_col] for row in all_values[1:] if len(row) > cae_col and row[cae_col]}
    except ValueError:
        return set()


def write_header_if_needed(worksheet):
    """Escribe la cabecera si la hoja está vacía."""
    first_row = worksheet.row_values(1)
    if not first_row:
        worksheet.append_row(COLUMNS, value_input_option="RAW")
        print("  → Cabecera escrita")


def append_comprobantes(worksheet, comprobantes: list[dict], existing_caes: set) -> int:
    """Agrega los comprobantes nuevos al sheet. Devuelve cuántos se agregaron."""
    rows_to_add = []
    for comp in comprobantes:
        cae = comp.get("Cód. Autorización", "")
        if cae and cae in existing_caes:
            continue  # ya existe, salteamos
        row = [comp.get(col, "") for col in COLUMNS]
        rows_to_add.append(row)

    if rows_to_add:
        worksheet.append_rows(rows_to_add, value_input_option="RAW")

    return len(rows_to_add)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Fecha de ayer en formato dd/mm/yyyy
    yesterday = date.today() - timedelta(days=1)
    fecha_str = yesterday.strftime("%d/%m/%Y")
    print(f"📅 Sincronizando comprobantes del {fecha_str}\n")

    # Variables de entorno
    afipsdk_token = os.environ["AFIPSDK_TOKEN"]
    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    credentials_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    clients = json.loads(os.environ["CLIENTS_JSON"])

    # clients debe ser una lista como:
    # [
    #   {"nombre": "Cliente A", "cuit": "20111111112", "password": "clave123"},
    #   ...
    # ]

    total_nuevos = 0

    for client in clients:
        nombre = client["nombre"]
        cuit = client["cuit"]
        password = client["password"]
        sheet_name = f"{nombre} - Recibidos"

        print(f"━━━ {nombre} ({cuit}) ━━━")

        try:
            # 1. Ejecutar automatización en Afip SDK
            automation_id = create_automation(afipsdk_token, cuit, cuit, password, fecha_str)
            comprobantes = poll_automation(afipsdk_token, automation_id)

            if not comprobantes:
                print(f"  → Sin comprobantes para el {fecha_str}")
                print()
                continue

            # 2. Conectar al Google Sheet
            worksheet = get_sheet(spreadsheet_id, sheet_name, credentials_json)
            write_header_if_needed(worksheet)
            existing_caes = get_existing_caes(worksheet)

            # 3. Escribir filas nuevas
            nuevos = append_comprobantes(worksheet, comprobantes, existing_caes)
            total_nuevos += nuevos
            print(f"  → {nuevos} comprobante(s) nuevo(s) agregado(s) al sheet")

        except Exception as e:
            print(f"  ❌ Error con {nombre}: {e}")

        print()

    print(f"✅ Sincronización completada. Total nuevos: {total_nuevos}")


if __name__ == "__main__":
    main()
