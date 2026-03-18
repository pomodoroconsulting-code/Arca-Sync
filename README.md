# ARCA Comprobantes → Google Sheets

Sincronización automática diaria de comprobantes recibidos de ARCA (ex-AFIP)
hacia Google Sheets, usando Afip SDK y GitHub Actions.

## ¿Cómo funciona?

Cada día a las 8:00 AM (Argentina), GitHub Actions ejecuta el script que:
1. Descarga los comprobantes recibidos del día anterior para cada cliente
2. Los agrega al Google Sheet (una pestaña por cliente)
3. Evita duplicados usando el CAE como identificador único

---

## Setup paso a paso

### 1. Crear cuenta en Afip SDK
- Ir a https://afipsdk.com y registrarse
- Obtener el `access_token` desde el dashboard
- Contratar el plan **Pro** ($25 USD/mes, hasta 10 CUITs)
- Agregar el add-on de **Automatizaciones** (100 gratis/mes, luego $50/mes)

### 2. Crear el Google Sheet
- Crear un nuevo Google Sheet en https://sheets.google.com
- Copiar el **ID** de la URL: `https://docs.google.com/spreadsheets/d/**ESTE_ES_EL_ID**/edit`

### 3. Crear Service Account de Google
1. Ir a https://console.cloud.google.com
2. Crear un proyecto nuevo (o usar uno existente)
3. Activar la **Google Sheets API** y la **Google Drive API**
4. Ir a "Credenciales" → "Crear credenciales" → "Cuenta de servicio"
5. Descargar el archivo JSON de la cuenta de servicio
6. **Compartir** el Google Sheet con el email de la service account
   (tiene el formato `nombre@proyecto.iam.gserviceaccount.com`) con permisos de **Editor**

### 4. Crear el repositorio en GitHub
1. Crear un repositorio nuevo (puede ser privado)
2. Subir los archivos:
   ```
   sync_comprobantes.py
   .github/workflows/daily_sync.yml
   ```

### 5. Configurar los Secrets en GitHub
Ir a: **Settings → Secrets and variables → Actions → New repository secret**

Crear los siguientes secrets:

| Secret | Descripción | Ejemplo |
|--------|-------------|---------|
| `AFIPSDK_TOKEN` | Token de Afip SDK | `eyJhbGc...` |
| `SPREADSHEET_ID` | ID del Google Sheet | `1BxiMVs0XRA...` |
| `GOOGLE_CREDENTIALS` | Contenido completo del JSON de la service account | `{"type": "service_account", ...}` |
| `CLIENTS_JSON` | Lista de clientes en formato JSON | ver abajo |

#### Formato de CLIENTS_JSON
```json
[
  {
    "nombre": "Cliente A",
    "cuit": "20111111112",
    "password": "clave_fiscal_cliente_a"
  },
  {
    "nombre": "Cliente B",
    "cuit": "27222222223",
    "password": "clave_fiscal_cliente_b"
  },
  {
    "nombre": "Cliente C",
    "cuit": "30333333334",
    "password": "clave_fiscal_cliente_c"
  }
]
```

> ⚠️ Los secrets de GitHub están cifrados y nunca se muestran en los logs.

### 6. Probar manualmente
1. Ir a la pestaña **Actions** del repositorio
2. Seleccionar el workflow "Sync Comprobantes ARCA"
3. Hacer click en **Run workflow**
4. Verificar que corra sin errores y que aparezcan datos en el Sheet

---

## Estructura del Google Sheet

Se crea automáticamente una pestaña por cliente con el nombre:
`{Nombre del Cliente} - Recibidos`

Columnas generadas:

| Columna | Descripción |
|---------|-------------|
| Fecha de Emisión | Fecha del comprobante |
| Tipo de Comprobante | Código numérico del tipo |
| Punto de Venta | Número de punto de venta del emisor |
| Número Desde / Hasta | Numeración del comprobante |
| Cód. Autorización | CAE (identificador único) |
| Tipo Doc. Receptor | Tipo de documento (80 = CUIT) |
| Nro. Doc. Receptor | CUIT/DNI del receptor |
| Denominación Receptor | Nombre/razón social del receptor |
| Moneda | Código de moneda (PES, DOL, etc.) |
| Tipo Cambio | Cotización utilizada |
| Imp. Neto Gravado | Monto neto gravado |
| Imp. Neto No Gravado | Monto neto no gravado |
| Imp. Op. Exentas | Operaciones exentas |
| Otros Tributos | Otros tributos |
| IVA | Monto de IVA |
| Imp. Total | **Importe total del comprobante** |

---

## Costos estimados

| Servicio | Costo |
|----------|-------|
| Afip SDK Plan Pro (hasta 10 CUITs) | $25 USD/mes |
| Automatizaciones (hasta 100/mes) | Gratis |
| Automatizaciones (hasta 1.000/mes) | $50 USD/mes |
| GitHub Actions | Gratis |
| Google Sheets | Gratis |

> Con 5 clientes y descarga diaria = ~150 automatizaciones/mes → $50 USD/mes adicionales.
> Si se baja a cada 2-3 días → se mantiene en las 100 gratuitas.

---

## Preguntas frecuentes

**¿Se pueden cargar comprobantes de fechas anteriores?**
Sí, modificá el script cambiando `fecha_str` por el rango deseado. También podés correrlo manualmente desde GitHub Actions.

**¿Qué pasa si la clave fiscal de un cliente cambia?**
Actualizá el secret `CLIENTS_JSON` con la nueva contraseña.

**¿Se pueden agregar más clientes?**
Sí, simplemente agregá más entradas al JSON de `CLIENTS_JSON`.
