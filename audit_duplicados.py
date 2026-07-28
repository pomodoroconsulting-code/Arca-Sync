"""
audit_duplicados.py — READ ONLY. No modifica el Sheet.
Compara duplicados bajo dos criterios de llave y muestra filas de ejemplo,
para distinguir duplicados REALES de comprobantes distintos que comparten CAEA.
"""
import os
import json
from collections import Counter, defaultdict

import gspread
from google.oauth2.service_account import Credentials

from sync_comprobantes import _norm

# Llave compuesta (única SIEMPRE, con o sin CAEA): identifica el comprobante real
COMP_FIELDS = ("Razón Social", "Tipo de Comprobante", "Punto de Venta",
               "Número Desde", "Nro. Doc. Emisor")
SHOW = ("Razón Social", "Fecha de Emisión", "Tipo de Comprobante",
        "Punto de Venta", "Número Desde", "Cód. Autorización",
        "Nro. Doc. Emisor", "Imp. Total")


def comp_key(row, idx):
    return "|".join(_norm(row[idx[n]]) if n in ("Punto de Venta", "Número Desde",
                    "Nro. Doc. Emisor") else str(row[idx[n]]).strip()
                    for n in COMP_FIELDS)


def cae_key(row, idx):
    return f"{str(row[idx['Cód. Autorización']]).strip()}|{str(row[idx['Razón Social']]).strip()}"


def audit_tab(ws):
    rows = ws.get_all_values()
    if len(rows) < 2:
        print("  (vacía)"); return
    header = rows[0]
    idx = {n: header.index(n) for n in header}
    data = rows[1:]

    comp_groups = defaultdict(list)
    cae_counter = Counter()
    blank_cae = 0
    for r in data:
        comp_groups[comp_key(r, idx)].append(r)
        c = str(r[idx["Cód. Autorización"]]).strip()
        if c:
            cae_counter[cae_key(r, idx)] += 1
        else:
            blank_cae += 1

    comp_dups = {k: v for k, v in comp_groups.items() if len(v) > 1}
    comp_extra = sum(len(v) - 1 for v in comp_dups.values())
    cae_dups = {k: n for k, n in cae_counter.items() if n > 1}
    cae_extra = sum(n - 1 for n in cae_dups.values())

    print(f"  Filas de datos: {len(data)} | sin CAE: {blank_cae}")
    print(f"  Duplicados por CAE:       {len(cae_dups)} llaves / {cae_extra} filas sobrantes")
    print(f"  Duplicados REALES (comp): {len(comp_dups)} llaves / {comp_extra} filas sobrantes")

    # Duplicados REALES: mostrar filas exactas (número de fila en el Sheet)
    if comp_dups:
        print("\n  — DUPLICADOS REALES (mismo comprobante repetido) —")
        show_i = [idx[c] for c in SHOW]
        for k, group in comp_dups.items():
            positions = [i + 2 for i, r in enumerate(data) if comp_key(r, idx) == k]
            print(f"\n  Filas del Sheet {positions} — mismo comprobante:")
            for r in group:
                print("    " + " | ".join(str(r[i]) for i in show_i))

    # ¿los dups por CAE son mismo comprobante o comprobantes distintos (CAEA)?
    print("\n  — Muestra de llaves CAE duplicadas (¿mismo comprobante o CAEA?) —")
    show_i = [idx[c] for c in SHOW]
    for k in sorted(cae_dups, key=lambda x: -cae_counter[x])[:5]:
        cae_val, rs = k.split("|", 1)
        matches = [r for r in data if str(r[idx["Cód. Autorización"]]).strip() == cae_val
                   and str(r[idx["Razón Social"]]).strip() == rs]
        distinct_comp = len({comp_key(r, idx) for r in matches})
        verdict = "MISMO comprobante (dup real)" if distinct_comp == 1 else f"{distinct_comp} comprobantes DISTINTOS (CAEA)"
        print(f"\n  CAE {cae_val} | {rs} → {len(matches)} filas → {verdict}")
        for r in matches[:8]:
            print("    " + " | ".join(str(r[i]) for i in show_i))


def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    print(f"📊 {ss.title}\n")
    for name in ("Recibidos", "Emitidos"):
        print(f"━━━━━━ {name} ━━━━━━")
        try:
            audit_tab(ss.worksheet(name))
        except Exception as e:
            print(f"  error: {e}")
        print()


if __name__ == "__main__":
    main()
