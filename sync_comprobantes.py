"""
sync_comprobantes.py
--------------------
Descarga comprobantes recibidos Y emitidos de ARCA (via Afip SDK)
y los sincroniza en un Google Sheet con:
  - "{cliente} - Recibidos"   : comprobantes recibidos
  - "{cliente} - Emitidos"    : comprobantes emitidos
  - "{cliente} - Resumen IVA" : resumen mensual de IVA

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
from collections import defaultdict
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

RESUMEN_COLUMNS = [
    "Mes",
    "Imp. Total Emitidos",
    "Total IVA Débito Fiscal",
    "Imp. Total Recibidos",
    "Total IVA Crédito Fiscal",
    "Posición IVA (Débito - Crédito)",
]

MESES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

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


def mes_label(year: int, month: int) -> str:
    return f"{MESES[month]} {year}"


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
                "t": tipo,  # "R" = Recibidos, "E" = Emitidos
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
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=5000, cols=40)
        print(f"  → Pestaña '{sheet_name}' creada")
        return ws


def write_header_if_needed(worksheet, columns):
    if not worksheet.row_values(1):
        worksheet.append_row(columns, value_input_option="RAW")
        print("  → Cabecera escrita")


def apply_currency_format(spreadsheet, worksheet, columns, currency_set):
    try:
        col_indices = [i for i, c in enumerate(columns) if c in currency_set]
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


def get_existing_caes(worksheet):
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        return set()
    try:
        cae_col = all_values[0].index("Cód. Autorización")
        return {row[cae_col] for row in all_values[1:]
                if len(row) > cae_col and row[cae_col]}
    except ValueError:
        return set()


def append_comprobantes(worksheet, comprobantes, existing_caes):
    rows_to_add = []
    for comp in comprobantes:
        cae = comp.get("Cód. Autorización", "")
        if cae and cae in existing_caes:
            continue
        rows_to_add.append(format_row(comp))
    if rows_to_add:
        worksheet.append_rows(rows_to_add, value_input_option="USER_ENTERED")
    return len(rows_to_add)


# ─── Resumen IVA ──────────────────────────────────────────────────────────────

def build_monthly_summary(emitidos: list, recibidos: list) -> list[dict]:
    """Agrupa emitidos y recibidos por mes y calcula los totales."""
    summary = defaultdict(lambda: {
        "imp_total_emitidos": 0.0,
        "iva_debito": 0.0,
        "imp_total_recibidos": 0.0,
        "iva_credito": 0.0,
    })

    for comp in emitidos:
        fecha = comp.get("Fecha de Emisión", "")
        if not fecha or len(fecha) < 7:
            continue
        year, month = int(fecha[:4]), int(fecha[5:7])
        key = (year, month)
        summary[key]["imp_total_emitidos"] += parse_amount(comp.get("Imp. Total", 0))
        summary[key]["iva_debito"] += parse_amount(comp.get("Total IVA", 0))

    for comp in recibidos:
        fecha = comp.get("Fecha de Emisión", "")
        if not fecha or len(fecha) < 7:
            continue
        year, month = int(fecha[:4]), int(fecha[5:7])
        key = (year, month)
        summary[key]["imp_total_recibidos"] += parse_amount(comp.get("Imp. Total", 0))
        summary[key]["iva_credito"] += parse_amount(comp.get("Total IVA", 0))

    result = []
    for (year, month) in sorted(summary.keys()):
        d = summary[(year, month)]
        result.append({
            "mes": mes_label(year, month),
            "year": year,
            "month": month,
            "imp_total_emitidos": d["imp_total_emitidos"],
            "iva_debito": d["iva_debito"],
            "imp_total_recibidos": d["imp_total_recibidos"],
            "iva_credito": d["iva_credito"],
            "posicion": d["iva_debito"] - d["iva_credito"],
        })
    return result


def update_resumen_sheet(spreadsheet, sheet_name, monthly_summary):
    """Actualiza (o crea) la pestaña de resumen IVA."""
    worksheet = get_or_create_worksheet(spreadsheet, sheet_name)

    # Leer datos actuales
    all_values = worksheet.get_all_values()

    # Construir dict de filas existentes por mes
    existing_rows = {}  # mes_label -> row_index (1-based)
    if len(all_values) >= 2:
        for i, row in enumerate(all_values[1:], start=2):
            if row:
                existing_rows[row[0]] = i

    # Escribir cabecera si no existe
    if not all_values or all_values[0] != RESUMEN_COLUMNS:
        if not all_values:
            worksheet.append_row(RESUMEN_COLUMNS, value_input_option="RAW")
        else:
            worksheet.update("A1", [RESUMEN_COLUMNS], value_input_option="RAW")
        print(f"  → Cabecera resumen escrita")

    # Formatear columnas numéricas del resumen
    resumen_currency = {
        "Imp. Total Emitidos",
        "Total IVA Débito Fiscal",
        "Imp. Total Recibidos",
        "Total IVA Crédito Fiscal",
        "Posición IVA (Débito - Crédito)",
    }
    apply_currency_format(spreadsheet, worksheet, RESUMEN_COLUMNS, resumen_currency)

    rows_updated = 0
    rows_added = 0

    for entry in monthly_summary:
        new_row = [
            entry["mes"],
            entry["imp_total_emitidos"],
            entry["iva_debito"],
            entry["imp_total_recibidos"],
            entry["iva_credito"],
            entry["posicion"],
        ]
        if entry["mes"] in existing_rows:
            # Actualizar fila existente
            row_idx = existing_rows[entry["mes"]]
            worksheet.update(f"A{row_idx}", [new_row], value_input_option="USER_ENTERED")
            rows_updated += 1
        else:
            # Agregar fila nueva
            worksheet.append_row(new_row, value_input_option="USER_ENTERED")
            rows_added += 1

    print(f"  → Resumen: {rows_added} mes(es) nuevo(s), {rows_updated} actualizado(s)")


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
            nombre     = entity["nombre"]

            print(f"━━━ {nombre} ({cuit}) ━━━")

            try:
                # ── 1. Descargar Recibidos ──
                print(f"  [Recibidos]")
                recibidos_nuevos_raw = fetch_chunked(
                    afipsdk_token, cuit, username, password,
                    fecha_desde, fecha_hasta, "R"
                )
                ws_rec = get_or_create_worksheet(spreadsheet, f"{nombre} - Recibidos")
                write_header_if_needed(ws_rec, COLUMNS)
                apply_currency_format(spreadsheet, ws_rec, COLUMNS, CURRENCY_FIELDS)
                caes_rec = get_existing_caes(ws_rec)
                nuevos_rec = append_comprobantes(ws_rec, recibidos_nuevos_raw, caes_rec)
                total_nuevos += nuevos_rec
                print(f"  → {nuevos_rec} recibido(s) nuevo(s)")

                # ── 2. Descargar Emitidos ──
                print(f"  [Emitidos]")
                emitidos_nuevos_raw = fetch_chunked(
                    afipsdk_token, cuit, username, password,
                    fecha_desde, fecha_hasta, "E"
                )
                ws_emi = get_or_create_worksheet(spreadsheet, f"{nombre} - Emitidos")
                write_header_if_needed(ws_emi, COLUMNS)
                apply_currency_format(spreadsheet, ws_emi, COLUMNS, CURRENCY_FIELDS)
                caes_emi = get_existing_caes(ws_emi)
                nuevos_emi = append_comprobantes(ws_emi, emitidos_nuevos_raw, caes_emi)
                total_nuevos += nuevos_emi
                print(f"  → {nuevos_emi} emitido(s) nuevo(s)")

                # ── 3. Actualizar Resumen IVA ──
                # Leer TODOS los datos históricos para recalcular el resumen completo
                print(f"  [Resumen IVA]")
                todos_recibidos = [
                    dict(zip(ws_rec.row_values(1), row))
                    for row in ws_rec.get_all_values()[1:]
                    if any(row)
                ]
                todos_emitidos = [
                    dict(zip(ws_emi.row_values(1), row))
                    for row in ws_emi.get_all_values()[1:]
                    if any(row)
                ]
                monthly = build_monthly_summary(todos_emitidos, todos_recibidos)
                update_resumen_sheet(spreadsheet, f"{nombre} - Resumen IVA", monthly)

            except Exception as e:
                print(f"  ❌ Error: {e}")

            print()

    print(f"✅ Listo. Total nuevos: {total_nuevos}")


if __name__ == "__main__":
    main()
