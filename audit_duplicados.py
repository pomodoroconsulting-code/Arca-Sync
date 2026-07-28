"""
audit_duplicados.py — READ ONLY. No modifica el Sheet.
Cuenta comprobantes duplicados por la misma llave de dedup que usa sync_comprobantes.
"""
import os
import json
from collections import Counter

import gspread
from google.oauth2.service_account import Credentials

from sync_comprobantes import build_key, KEY_FIELDS


def audit_tab(ws):
    rows = ws.get_all_values()
    if len(rows) < 2:
        print("  (pestaña vacía)")
        return
    header = rows[0]
    try:
        idx = {n: header.index(n) for n in KEY_FIELDS}
    except ValueError as e:
        print(f"  ⚠ falta columna: {e}")
        return
    cae_i = header.index("Cód. Autorización")

    keys, blank_cae = [], 0
    for r in rows[1:]:
        vals = [r[idx[n]] if len(r) > idx[n] else "" for n in KEY_FIELDS]
        keys.append(build_key(*vals))
        if not (r[cae_i].strip() if len(r) > cae_i else ""):
            blank_cae += 1

    c = Counter(keys)
    dups = {k: n for k, n in c.items() if n > 1}
    extra = sum(n - 1 for n in dups.values())

    print(f"  Filas de datos:        {len(rows) - 1}")
    print(f"  Comprobantes únicos:   {len(c)}")
    print(f"  Llaves con duplicados: {len(dups)}")
    print(f"  → Filas sobrantes (a eliminar): {extra}")
    print(f"  Filas sin CAE:         {blank_cae}")
    for k, n in sorted(dups.items(), key=lambda x: -x[1])[:8]:
        print(f"    · {n}× {k}")


def main():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    print(f"📊 {ss.title}\n")
    for name in ("Recibidos", "Emitidos"):
        print(f"━━━ {name} ━━━")
        try:
            audit_tab(ss.worksheet(name))
        except Exception as e:
            print(f"  error: {e}")
        print()


if __name__ == "__main__":
    main()
