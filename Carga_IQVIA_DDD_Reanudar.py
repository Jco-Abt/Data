import csv
import json
import uuid
import getpass
import pyodbc

from pathlib import Path
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext


# ============================================================
# 1. CONFIGURACION GENERAL
# ============================================================

ARCHIVO = r"\\oneabbott.com\misc\PE\EPD\BEXPERU\Data_Warehouse\IQVIA_AUDITORIAS\IQVIA_DDD\Dataset_ddd_202609.txt"

AUDIT_DATE = date(2026, 9, 1)

# None = procesar el archivo completo.
LIMITE_REGISTROS = None

TAMANO_LOTE = 5_000

# Reutiliza el LOAD_RUN_ID de la carga interrumpida.
# Para una carga nueva, cambia este valor a None.
LOAD_RUN_ID_REANUDAR = "c5066572-7c42-494e-800e-5e259cfc07d0"

# Tamano de cada actualizacion SQL del USD corporativo.
TAMANO_LOTE_USD = 100_000

TABLA_STAGING = "dbo.Stg_DDD"
TABLA_ERRORES = "dbo.Log_Errores_DDD"

COLUMNAS_ESPERADAS = [
    "BRICK_COD",
    "PACKS",
    "IDPERIODO",
    "SOLES",
    "UN",
    "USD",
    "UNIDAD_CUENTA"
]

LIMITE_ENTEROS = Decimal("1E11")
UNIDAD_ESCALA = Decimal("0." + "0" * 18 + "1")


# ============================================================
# 2. CONEXION SQL SERVER
# ============================================================

SERVIDOR = "SERVER"
BASE_DATOS = "BASE"
USUARIO = "USER"

# Solicita la contraseña al iniciar; no queda escrita en el archivo.
PASSWORD = getpass.getpass("Contraseña de SQL Server: ")

CONNECTION_STRING = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={SERVIDOR};"
    f"DATABASE={BASE_DATOS};"
    f"UID={USUARIO};"
    f"PWD={PASSWORD};"
    "Encrypt=yes;"
    "TrustServerCertificate=yes;"
)

# TrustServerCertificate=yes conserva la configuracion usada en las pruebas.
# En produccion, lo ideal es configurar y validar el certificado del servidor.


# ============================================================
# 3. FUNCIONES DE CONVERSION
# ============================================================

def convertir_decimal(valor):

    valor = str(valor).strip()

    if not valor:
        raise ValueError("Valor numerico vacio")

    try:
        numero = Decimal(valor)
    except InvalidOperation:
        raise ValueError(f"Valor numerico invalido: {valor}")

    if not numero.is_finite():
        raise ValueError(f"Valor numerico no finito: {valor}")

    # DECIMAL(30,19) admite hasta 11 digitos enteros.
    if abs(numero) >= LIMITE_ENTEROS:
        raise ValueError(f"Excede el rango de DECIMAL(30,19): {valor}")

    # Verifica que no se pierda precision al limitar a 19 decimales.
    with localcontext() as contexto:
        contexto.prec = 60
        numero_ajustado = numero.quantize(UNIDAD_ESCALA)

        if numero != numero_ajustado:
            raise ValueError(f"Excede los 19 decimales permitidos: {valor}")

    return numero_ajustado


def convertir_entero(valor, columna):

    numero = convertir_decimal(valor)

    if numero != numero.to_integral_value():
        raise ValueError(f"{columna} debe ser un numero entero: {valor}")

    resultado = int(numero)

    if columna == "BRICK_COD":
        if resultado < 0 or resultado > 2147483647:
            raise ValueError(f"BRICK_COD fuera del rango INT: {valor}")

    elif columna == "IDPERIODO":
        if resultado < 1 or resultado > 60:
            raise ValueError(f"IDPERIODO debe estar entre 1 y 60: {valor}")

    return resultado


# ============================================================
# 4. CONSULTAR TIPO DE CAMBIO CORPORATIVO
# ============================================================

def obtener_tipo_cambio(cursor):

    periodo = AUDIT_DATE.year

    cursor.execute(
        """
        SELECT TIPO_CAMBIO
        FROM dbo.Dim_Tipo_Cambio
        WHERE PERIODO = ?
        """,
        periodo
    )

    resultados = cursor.fetchall()

    if len(resultados) != 1:
        raise ValueError(
            f"Se esperaba exactamente un tipo de cambio para {periodo}. "
            f"Registros encontrados: {len(resultados)}."
        )

    tipo_cambio = Decimal(str(resultados[0][0]))

    if not tipo_cambio.is_finite() or tipo_cambio <= 0:
        raise ValueError(f"Tipo de cambio invalido para {periodo}")

    return tipo_cambio


# ============================================================
# 5. VALIDAR QUE NO EXISTA UNA CARGA ANTERIOR
# ============================================================

def validar_carga_anterior(cursor, nombre_archivo):

    cursor.execute(
        f"""
        SELECT COUNT_BIG(*)
        FROM {TABLA_STAGING}
        WHERE AUDIT_DATE = ?
          AND SOURCE_FILE = ?
        """,
        AUDIT_DATE,
        nombre_archivo
    )

    cantidad = cursor.fetchone()[0]

    if cantidad > 0:
        raise RuntimeError(
            f"Ya existen {cantidad:,} registros para "
            f"{nombre_archivo} y auditoria {AUDIT_DATE}.\n"
            "Se cancela la carga para evitar duplicados. "
            "Si es una carga interrumpida, debe revisarse antes de reintentar."
        )


# ============================================================
# 6. SENTENCIAS SQL
# ============================================================

SQL_INSERT_STAGING = f"""
INSERT INTO {TABLA_STAGING}
(
    LOAD_RUN_ID,
    SOURCE_ROW_NUMBER,
    SOURCE_FILE,
    AUDIT_DATE,
    BRICK_COD,
    PACKS,
    IDPERIODO,
    SOLES,
    UN,
    USD_IQVIA,
    UNIDAD_CUENTA
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SQL_INSERT_ERRORES = f"""
INSERT INTO {TABLA_ERRORES}
(
    LOAD_RUN_ID,
    SOURCE_ROW_NUMBER,
    SOURCE_FILE,
    AUDIT_DATE,
    ERROR_COLUMN,
    ERROR_MESSAGE,
    RAW_ROW
)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


# ============================================================
# 7. PROCESAR UN REGISTRO
# ============================================================

def procesar_fila(fila, numero_fila, nombre_archivo, load_run_id):

    errores = []

    # Comprueba columnas adicionales o faltantes.
    if None in fila or any(valor is None for valor in fila.values()):
        return None, (
            load_run_id,
            numero_fila,
            nombre_archivo,
            AUDIT_DATE,
            "ESTRUCTURA",
            "La fila contiene columnas faltantes o adicionales.",
            json.dumps(fila, ensure_ascii=False, default=str)
        )

    try:
        brick_cod = convertir_entero(fila["BRICK_COD"], "BRICK_COD")
    except (ValueError, InvalidOperation) as error:
        errores.append(("BRICK_COD", str(error)))

    packs = str(fila["PACKS"]).strip()

    if not packs:
        errores.append(("PACKS", "PACKS esta vacio"))
    elif len(packs) > 50:
        errores.append(("PACKS", "PACKS supera los 50 caracteres"))

    try:
        idperiodo = convertir_entero(fila["IDPERIODO"], "IDPERIODO")
    except (ValueError, InvalidOperation) as error:
        errores.append(("IDPERIODO", str(error)))

    valores = {}

    for columna in ["SOLES", "UN", "USD", "UNIDAD_CUENTA"]:
        try:
            valores[columna] = convertir_decimal(fila[columna])
        except (ValueError, InvalidOperation) as error:
            errores.append((columna, str(error)))

    if errores:
        columnas_error = ", ".join(columna for columna, mensaje in errores)
        mensajes = " | ".join(
            f"{columna}: {mensaje}" for columna, mensaje in errores
        )

        registro_error = (
            load_run_id,
            numero_fila,
            nombre_archivo,
            AUDIT_DATE,
            columnas_error[:100],
            mensajes[:1000],
            json.dumps(fila, ensure_ascii=False, default=str)
        )

        return None, registro_error

    registro_valido = (
        load_run_id,
        numero_fila,
        nombre_archivo,
        AUDIT_DATE,
        brick_cod,
        packs,
        idperiodo,
        valores["SOLES"],
        valores["UN"],
        valores["USD"],
        valores["UNIDAD_CUENTA"]
    )

    return registro_valido, None


# ============================================================
# 8. INSERTAR LOTES
# ============================================================

def insertar_lote(cursor, conexion, registros_validos, registros_error):

    if registros_validos:
        cursor.executemany(SQL_INSERT_STAGING, registros_validos)

    if registros_error:
        cursor.executemany(SQL_INSERT_ERRORES, registros_error)

    # Cada lote queda confirmado independientemente.
    conexion.commit()


# ============================================================
# 9. CALCULAR USD CORPORATIVO
# ============================================================

def calcular_usd_corporativo(cursor, conexion, load_run_id, tipo_cambio):

    print("\\nCalculando USD corporativo por lotes...")

    total_actualizado = 0

    while True:
        sql = f"""
        DECLARE @TC DECIMAL(10,4) = ?;
        DECLARE @ACTUALIZADOS INT;

        UPDATE TOP ({TAMANO_LOTE_USD}) {TABLA_STAGING}
        SET
            TIPO_CAMBIO_CORP = @TC,
            USD_CORPORATIVO =
                CAST(SOLES / @TC AS DECIMAL(30,19))
        WHERE LOAD_RUN_ID = ?
          AND (
                TIPO_CAMBIO_CORP IS NULL
                OR USD_CORPORATIVO IS NULL
          );

        SET @ACTUALIZADOS = @@ROWCOUNT;
        SELECT @ACTUALIZADOS;
        """

        cursor.execute(sql, tipo_cambio, load_run_id)
        actualizados = cursor.fetchone()[0]
        conexion.commit()

        if actualizados == 0:
            break

        total_actualizado += actualizados
        print(
            f"Registros con USD corporativo actualizado: "
            f"{total_actualizado:,}"
        )

    cursor.execute(
        f"""
        SELECT
            COUNT_BIG(*) AS TOTAL,
            SUM(
                CASE
                    WHEN TIPO_CAMBIO_CORP IS NULL
                      OR USD_CORPORATIVO IS NULL
                    THEN 1
                    ELSE 0
                END
            ) AS PENDIENTES
        FROM {TABLA_STAGING}
        WHERE LOAD_RUN_ID = ?
        """,
        load_run_id
    )

    total, pendientes = cursor.fetchone()
    pendientes = pendientes or 0

    if pendientes:
        raise RuntimeError(
            f"Hay {pendientes:,} registros sin calculo corporativo."
        )

    print(
        f"USD corporativo validado: {total:,} registros. "
        f"Actualizados en esta ejecucion: {total_actualizado:,}."
    )


def obtener_ultima_fila_procesada(cursor, load_run_id):

    cursor.execute(
        f"""
        SELECT MAX(SOURCE_ROW_NUMBER)
        FROM
        (
            SELECT SOURCE_ROW_NUMBER
            FROM {TABLA_STAGING}
            WHERE LOAD_RUN_ID = ?

            UNION ALL

            SELECT SOURCE_ROW_NUMBER
            FROM {TABLA_ERRORES}
            WHERE LOAD_RUN_ID = ?
        ) AS R
        """,
        load_run_id,
        load_run_id
    )

    resultado = cursor.fetchone()[0]

    if resultado is None:
        raise RuntimeError(
            "No se encontraron registros previos para el LOAD_RUN_ID "
            "indicado. Verifica el identificador antes de continuar."
        )

    return int(resultado)


def validar_ejecucion_a_reanudar(cursor, load_run_id, nombre_archivo):

    cursor.execute(
        f"""
        SELECT COUNT_BIG(*)
        FROM {TABLA_STAGING}
        WHERE LOAD_RUN_ID = ?
          AND AUDIT_DATE = ?
          AND SOURCE_FILE = ?
        """,
        load_run_id,
        AUDIT_DATE,
        nombre_archivo
    )
    cantidad_staging = cursor.fetchone()[0]

    cursor.execute(
        f"""
        SELECT COUNT_BIG(*)
        FROM {TABLA_ERRORES}
        WHERE LOAD_RUN_ID = ?
          AND AUDIT_DATE = ?
          AND SOURCE_FILE = ?
        """,
        load_run_id,
        AUDIT_DATE,
        nombre_archivo
    )
    cantidad_errores = cursor.fetchone()[0]

    if cantidad_staging + cantidad_errores == 0:
        raise RuntimeError(
            "El LOAD_RUN_ID no tiene registros para esta auditoria y archivo."
        )


# ============================================================
# 10. PROCESO PRINCIPAL
# ============================================================

def ejecutar_carga():

    ruta = Path(ARCHIVO)

    if not ruta.exists():
        raise FileNotFoundError(f"No existe el archivo: {ruta}")

    nombre_archivo = ruta.name

    reanudando = LOAD_RUN_ID_REANUDAR is not None
    load_run_id = (
        LOAD_RUN_ID_REANUDAR
        if reanudando
        else str(uuid.uuid4())
    )

    total_leidos = 0
    total_insertados = 0
    total_rechazados = 0

    registros_validos = []
    registros_error = []

    print("\\n" + "=" * 65)
    print("CARGA IQVIA DDD - " + ("REANUDACION" if reanudando else "NUEVA CARGA"))
    print("=" * 65)
    print(f"Archivo: {nombre_archivo}")
    print(f"Auditoria: {AUDIT_DATE}")
    print(f"LOAD_RUN_ID: {load_run_id}")
    print(f"Tamano de lote: {TAMANO_LOTE:,}")

    conexion = None

    try:
        conexion = pyodbc.connect(CONNECTION_STRING, autocommit=False)
        cursor = conexion.cursor()
        cursor.fast_executemany = True

        tipo_cambio = obtener_tipo_cambio(cursor)
        print(
            f"Tipo de cambio corporativo {AUDIT_DATE.year}: "
            f"{tipo_cambio}"
        )

        if reanudando:
            validar_ejecucion_a_reanudar(
                cursor,
                load_run_id,
                nombre_archivo
            )
            ultima_fila_confirmada = obtener_ultima_fila_procesada(
                cursor,
                load_run_id
            )
            print(
                f"Reanudando despues de la fila "
                f"{ultima_fila_confirmada:,} del archivo."
            )
        else:
            validar_carga_anterior(cursor, nombre_archivo)
            ultima_fila_confirmada = 1
            print("Iniciando una carga nueva desde la primera fila de datos.")

        with open(ruta, "r", encoding="utf-8-sig", newline="") as archivo:

            lector = csv.DictReader(archivo, delimiter=",")

            if lector.fieldnames != COLUMNAS_ESPERADAS:
                raise ValueError(
                    "Los encabezados del TXT no coinciden.\\n"
                    f"Encontrados: {lector.fieldnames}"
                )

            print("\\nComenzando lectura e insercion...\\n")

            for numero_fila, fila in enumerate(lector, start=2):

                # Omite las filas ya confirmadas en staging o en el log de errores.
                if numero_fila <= ultima_fila_confirmada:
                    continue

                if (
                    LIMITE_REGISTROS is not None
                    and total_leidos >= LIMITE_REGISTROS
                ):
                    break

                total_leidos += 1

                registro, error = procesar_fila(
                    fila,
                    numero_fila,
                    nombre_archivo,
                    load_run_id
                )

                if registro is not None:
                    registros_validos.append(registro)

                if error is not None:
                    registros_error.append(error)

                if (
                    len(registros_validos) + len(registros_error)
                    >= TAMANO_LOTE
                ):
                    insertar_lote(
                        cursor,
                        conexion,
                        registros_validos,
                        registros_error
                    )

                    total_insertados += len(registros_validos)
                    total_rechazados += len(registros_error)

                    registros_validos.clear()
                    registros_error.clear()

                    procesados = total_insertados + total_rechazados
                    print(
                        f"Nuevos procesados: {procesados:,} | "
                        f"Insertados: {total_insertados:,} | "
                        f"Rechazados: {total_rechazados:,} | "
                        f"Fila archivo: {numero_fila:,}"
                    )

            if registros_validos or registros_error:
                insertar_lote(
                    cursor,
                    conexion,
                    registros_validos,
                    registros_error
                )

                total_insertados += len(registros_validos)
                total_rechazados += len(registros_error)

                registros_validos.clear()
                registros_error.clear()

        # Completa el calculo para todo el LOAD_RUN_ID.
        # Si se interrumpe, al reanudar solo actualizara filas pendientes.
        calcular_usd_corporativo(
            cursor,
            conexion,
            load_run_id,
            tipo_cambio
        )

        print("\\n" + "=" * 65)
        print("RESUMEN DE ESTA EJECUCION")
        print("=" * 65)
        print(f"LOAD_RUN_ID: {load_run_id}")
        print(f"Nuevos registros leidos: {total_leidos:,}")
        print(f"Nuevos registros insertados: {total_insertados:,}")
        print(f"Nuevos registros rechazados: {total_rechazados:,}")

        porcentaje = (
            total_rechazados / total_leidos * 100
            if total_leidos > 0 else 0
        )
        print(f"Porcentaje de rechazo en esta reanudacion: {porcentaje:.6f}%")
        print("\\nProceso finalizado.")

    except Exception:
        if conexion is not None:
            try:
                conexion.rollback()
            except pyodbc.Error:
                # La conexion puede estar rota; los lotes previamente
                # confirmados permanecen en SQL Server.
                pass

        print("\\nERROR DURANTE LA CARGA")
        print(
            "Los lotes confirmados permanecen en SQL Server. "
            "Para continuar, vuelve a ejecutar este script con el mismo "
            "LOAD_RUN_ID_REANUDAR."
        )
        print(f"LOAD_RUN_ID: {load_run_id}")
        raise

    finally:
        if conexion is not None:
            try:
                conexion.close()
            except pyodbc.Error:
                pass


# ============================================================
# 11. EJECUTAR
# ============================================================

if __name__ == "__main__":
    ejecutar_carga()
