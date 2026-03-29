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
  - DAYS_BACK          : (opcional) dias hacia atras. Default: 3. Usar 45 para carga inicial.
"""

import os
import json
import time
import requests
from datetime import date, timedelta
from collections import defaultdict
import gspread
from google.oauth2.service_account import Credentials

AFIPSDK_BASE     = "https://app.afipsdk.com/api/v1"
POLL_INTERVAL    = 5
MAX_WAIT         = 300
MAX_RETRIES      = 3
RETRY_WAIT       = 15
CHUNK_DAYS       = 15
SHEETS_PAUSE     = 3

CURRENCY_FIELDS = {
    "Imp. Neto Gravado IVA 0%", "Imp. Neto Gravado IVA 2,5%",
    "Imp. Neto Gravado IVA 5%", "Imp. Neto Gravado IVA 10,5%",
    "Imp. Neto Gravado IVA 21%", "Imp. Neto Gravado IVA 27%",
    "Imp. Neto Gravado Total", "IVA 2,5%", "IVA 5%", "IVA 10,5%",
    "IVA 21%", "IVA 27%", "Total IVA", "Imp. Neto No Gravado",
    "Imp. Op. Exentas", "Otros Tributos", "Imp. Total",
}

COLUMNS = [
    "Fecha de Emision", "Tipo de Comprobante", "Punto de Venta",
    "Numero Desde", "Numero Hasta", "Cod. Autorizacion",
    "Tipo Doc. Emisor", "Nro. Doc. Emisor", "Denominacion Emisor",
    "Tipo Doc. Receptor", "Nro. Doc. Receptor", "Moneda", "Tipo Cambio",
    "Imp. Neto Gravado IVA 0%", "Imp. Neto Gravado IVA 2,5%", "IVA 2,5%",
    "Imp. Neto Gravado IVA 5%", "IVA 5%", "Imp. Neto Gravado IVA 10,5%",
    "IVA 10,5%", "Imp. Neto Gravado IVA 21%", "IVA 21%",
    "Imp. Neto Gravado IVA 27%", "IVA 27%", "Imp. Neto Gravado Total",
    "Imp. Neto No Gravado", "Imp. Op. Exentas", "Otros Tributos",
    "Total IVA", "Imp. Total",
]

# Mapa de nombres reales de ARCA -> nombres de columna
FIELD_MAP = {
    "Fecha de Emisión": "Fecha de Emision",
    "Número Desde": "Numero Desde",
    "Número Hasta": "Numero Hasta",
    "Cód. Autorización": "Cod. Autorizacion",
    "Denominación Emisor": "Denominacion Emisor",
}

RESUMEN_COLUMNS = [
    "Mes", "Imp. Total Emitidos", "Total IVA Debito Fiscal",
    "Imp. Total Recibidos", "Total IVA Credito Fiscal",
    "Posicion IVA (Debito - Credito)",
]

MESES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}


def pause(s=SHEETS_PAUSE):
    time.sleep(s)


def parse_amount(value) -> float:
    s = str(value).strip()
    if s in ("", "-", "0"):
        return 0.0
    cleaned = s.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def normalize_comp(comp: dict) -> dict:
    """Renombra campos de ARCA a nombres sin tildes/caracteres especiales."""
    out = {}
    for k, v in comp.items():
        out[FIELD_MAP.get(k, k)] = v
    return out


def format_row(comp: dict) -> list:
    comp = normalize_comp(comp)
    row = []
    for col in COLUMNS:
        val = comp.get(col, "")
        if col in CURRENCY_FIELDS:
            row.append(parse_amount(val))
        else:
            row.append(val if val is not None else "")
    return row


def date_chunks(fecha_desde, fecha_hasta, chunk_days):
    current = fecha_desde
    while current <= fecha_hasta:
        end = min(current + timedelta(days=chunk_days - 1), fecha_hasta)
        yield current, end
        current = end + timedelta(days=1)


def mes_label(year, month):
    return f"{MESES[month]} {year}"


def create_automation(token, cuit, username, password, desde_str, hasta_str, tipo):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "automation": "mis-comprobantes",
        "params": {
            "cuit": cuit, "username": username, "password": password,
            "filters": {"t": tipo, "fechaEmision": f"{desde_str} - {hasta_str}"},
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
            raise RuntimeError(f"Error ARCA: {data.get('data', {}).get('message', 'Error')}")
    raise TimeoutError(f"Timeout esperando {automation_id}")


def fetch_with_retry(token, cuit, username, password, desde_str, hasta_str, tipo):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  -> {tipo} | {desde_str} - {hasta_str} (intento {attempt})")
            aid = create_automation(token, cuit, username, password, desde_str, hasta_str, tipo)
            result = poll_automation(token, aid)
            print(f"     {len(result)} comprobante(s)")
            return result
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 500:
                print(f"     Error 500 (intento {attempt}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_WAIT)
                else:
                    raise RuntimeError(f"Fallo tras {MAX_RETRIES} intentos: {e}")
            else:
                raise
    return []


def fetch_chunked(token, cuit, username, password, fecha_desde, fecha_hasta, tipo):
    all_comp = []
    for s, e in date_chunks(fecha_desde, fecha_hasta, CHUNK_DAYS):
        all_comp.extend(fetch_with_retry(
            token, cuit, username, password,
            s.strftime("%d/%m/%Y"), e.strftime("%d/%m/%Y"), tipo
        ))
    return all_comp


def get_or_create_worksheet(spreadsheet, sheet_name):
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        pause()
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=5000, cols=40)
        print(f"  -> Pestana '{sheet_name}' creada")
        return ws


def write_header_if_needed(worksheet, columns):
    pause()
    if not worksheet.row_values(1):
        pause()
        worksheet.append_row(columns, value_input_option="RAW")
        print("  -> Cabecera escrita")


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
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        } for col_idx in col_indices]
        if reqs:
            pause()
            spreadsheet.batch_update({"requests": reqs})
    except Exception as e:
        print(f"  Formato moneda: {e}")


def get_existing_caes(worksheet) -> set:
    pause()
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        return set()
    try:
        cae_col = all_values[0].index("Cod. Autorizacion")
        return {row[cae_col] for row in all_values[1:] if len(row) > cae_col and row[cae_col]}
    except ValueError:
        return set()


def get_all_rows_as_dicts(worksheet) -> list:
    pause()
    all_values = worksheet.get_all_values()
    if len(all_values) < 2:
        return []
    header = all_values[0]
    return [dict(zip(header, row)) for row in all_values[1:] if any(row)]


def append_comprobantes(worksheet, comprobantes, existing_caes) -> int:
    rows_to_add = []
    for comp in comprobantes:
        cae = normalize_comp(comp).get("Cod. Autorizacion", "")
        if cae and cae in existing_caes:
            continue
        rows_to_add.append(format_row(comp))
    if rows_to_add:
        pause()
        worksheet.append_rows(rows_to_add, value_input_option="USER_ENTERED")
    return len(rows_to_add)


def build_monthly_summary(emitidos, recibidos):
    summary = defaultdict(lambda: {"imp_total_emitidos": 0.0, "iva_debito": 0.0,
                                    "imp_total_recibidos": 0.0, "iva_credito": 0.0})
    for comp in emitidos:
        fecha = comp.get("Fecha de Emision", "")
        if not fecha or len(fecha) < 7:
            continue
        try:
            year, month = int(fecha[:4]), int(fecha[5:7])
        except ValueError:
            continue
        summary[(year, month)]["imp_total_emitidos"] += parse_amount(comp.get("Imp. Total", 0))
        summary[(year, month)]["iva_debito"] += parse_amount(comp.get("Total IVA", 0))

    for comp in recibidos:
        fecha = comp.get("Fecha de Emision", "")
        if not fecha or len(fecha) < 7:
            continue
        try:
            year, month = int(fecha[:4]), int(fecha[5:7])
        except ValueError:
            continue
        summary[(year, month)]["imp_total_recibidos"] += parse_amount(comp.get("Imp. Total", 0))
        summary[(year, month)]["iva_credito"] += parse_amount(comp.get("Total IVA", 0))

    result = []
    for (year, month) in sorted(summary.keys()):
        d = summary[(year, month)]
        result.append({
            "mes": mes_label(year, month),
            "imp_total_emitidos": d["imp_total_emitidos"],
            "iva_debito": d["iva_debito"],
            "imp_total_recibidos": d["imp_total_recibidos"],
            "iva_credito": d["iva_credito"],
            "posicion": d["iva_debito"] - d["iva_credito"],
        })
    return result


def update_resumen_sheet(spreadsheet, sheet_name, monthly_summary):
    worksheet = get_or_create_worksheet(spreadsheet, sheet_name)
    pause()
    all_values = worksheet.get_all_values()

    if not all_values or all_values[0] != RESUMEN_COLUMNS:
        pause()
        if not all_values:
            worksheet.append_row(RESUMEN_COLUMNS, value_input_option="RAW")
        else:
            worksheet.update("A1", [RESUMEN_COLUMNS], value_input_option="RAW")
        pause()
        all_values = worksheet.get_all_values()
        print("  -> Cabecera resumen escrita")

    resumen_currency = {
        "Imp. Total Emitidos", "Total IVA Debito Fiscal",
        "Imp. Total Recibidos", "Total IVA Credito Fiscal",
        "Posicion IVA (Debito - Credito)",
    }
    apply_currency_format(spreadsheet, worksheet, RESUMEN_COLUMNS, resumen_currency)

    existing_rows = {}
    if len(all_values) >= 2:
        for i, row in enumerate(all_values[1:], start=2):
            if row:
                existing_rows[row[0]] = i

    rows_updated = rows_added = 0
    for entry in monthly_summary:
        new_row = [
            entry["mes"], entry["imp_total_emitidos"], entry["iva_debito"],
            entry["imp_total_recibidos"], entry["iva_credito"], entry["posicion"],
        ]
        pause()
        if entry["mes"] in existing_rows:
            worksheet.update(f"A{existing_rows[entry['mes']]}", [new_row], value_input_option="USER_ENTERED")
            rows_updated += 1
        else:
            worksheet.append_row(new_row, value_input_option="USER_ENTERED")
            rows_added += 1

    print(f"  -> Resumen: {rows_added} mes(es) nuevo(s), {rows_updated} actualizado(s)")


def main():
    days_back   = int(os.environ.get("DAYS_BACK", "3"))
    fecha_hasta = date.today() - timedelta(days=1)
    fecha_desde = fecha_hasta - timedelta(days=days_back - 1)

    print(f"Periodo: {fecha_desde.strftime('%d/%m/%Y')} -> {fecha_hasta.strftime('%d/%m/%Y')}")
    print(f"  ({days_back} dias, chunks de {CHUNK_DAYS})\n")

    afipsdk_token    = os.environ["AFIPSDK_TOKEN"]
    spreadsheet_id   = os.environ["SPREADSHEET_ID"]
    credentials_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    clients          = json.loads(os.environ["CLIENTS_JSON"])

    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
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
            cuit   = entity["cuit"]
            nombre = entity["nombre"]
            print(f"=== {nombre} ({cuit}) ===")

            try:
                print("  [Recibidos]")
                recibidos_raw = fetch_chunked(afipsdk_token, cuit, username, password,
                                              fecha_desde, fecha_hasta, "R")
                ws_rec = get_or_create_worksheet(spreadsheet, f"{nombre} - Recibidos")
                write_header_if_needed(ws_rec, COLUMNS)
                apply_currency_format(spreadsheet, ws_rec, COLUMNS, CURRENCY_FIELDS)
                nuevos_rec = append_comprobantes(ws_rec, recibidos_raw, get_existing_caes(ws_rec))
                total_nuevos += nuevos_rec
                print(f"  -> {nuevos_rec} recibido(s) nuevo(s)")

                print("  [Emitidos]")
                emitidos_raw = fetch_chunked(afipsdk_token, cuit, username, password,
                                             fecha_desde, fecha_hasta, "E")
                ws_emi = get_or_create_worksheet(spreadsheet, f"{nombre} - Emitidos")
                write_header_if_needed(ws_emi, COLUMNS)
                apply_currency_format(spreadsheet, ws_emi, COLUMNS, CURRENCY_FIELDS)
                nuevos_emi = append_comprobantes(ws_emi, emitidos_raw, get_existing_caes(ws_emi))
                total_nuevos += nuevos_emi
                print(f"  -> {nuevos_emi} emitido(s) nuevo(s)")

                print("  [Resumen IVA]")
                todos_recibidos = get_all_rows_as_dicts(ws_rec)
                todos_emitidos  = get_all_rows_as_dicts(ws_emi)
                monthly = build_monthly_summary(todos_emitidos, todos_recibidos)
                update_resumen_sheet(spreadsheet, f"{nombre} - Resumen IVA", monthly)

            except Exception as e:
                print(f"  ERROR: {e}")

            print()

    print(f"Listo. Total nuevos: {total_nuevos}")


if __name__ == "__main__":
    main()
