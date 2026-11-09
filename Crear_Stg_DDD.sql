/*
    Tabla de staging de produccion para IQVIA DDD.
    Nombre utilizado por el script Python: dbo.Stg_DDD

    Ejecutar una sola vez en la base de datos del Data Warehouse.
*/

IF OBJECT_ID(N'dbo.Stg_DDD', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Stg_DDD
    (
        LOAD_RUN_ID          VARCHAR(36)       NOT NULL,
        SOURCE_ROW_NUMBER    BIGINT            NOT NULL,
        SOURCE_FILE          VARCHAR(260)      NOT NULL,
        AUDIT_DATE           DATE              NOT NULL,

        BRICK_COD            INT               NOT NULL,
        PACKS                VARCHAR(50)       NOT NULL,
        IDPERIODO            TINYINT           NOT NULL,

        SOLES                DECIMAL(30,19)    NOT NULL,
        UN                   DECIMAL(30,19)    NOT NULL,
        USD_IQVIA            DECIMAL(30,19)    NOT NULL,
        UNIDAD_CUENTA        DECIMAL(30,19)    NOT NULL,

        TIPO_CAMBIO_CORP     DECIMAL(10,4)     NULL,
        USD_CORPORATIVO      DECIMAL(30,19)    NULL,

        LOADED_AT             DATETIME2(0)      NOT NULL
            CONSTRAINT DF_Stg_DDD_LOADED_AT DEFAULT SYSDATETIME()
    );

    PRINT 'Tabla dbo.Stg_DDD creada correctamente.';
END
ELSE
BEGIN
    PRINT 'La tabla dbo.Stg_DDD ya existe. No se realizaron cambios.';
END;
GO

/*
    Indice para localizar las cargas por auditoria y archivo,
    utilizado por la validacion anti-duplicados del script.
*/
IF NOT EXISTS
(
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.Stg_DDD')
      AND name = N'IX_Stg_DDD_Audit_Source'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_Stg_DDD_Audit_Source
        ON dbo.Stg_DDD (AUDIT_DATE, SOURCE_FILE);
END;
GO

/*
    Indice para localizar y actualizar los registros de una ejecucion.
*/
IF NOT EXISTS
(
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID(N'dbo.Stg_DDD')
      AND name = N'IX_Stg_DDD_LoadRun'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_Stg_DDD_LoadRun
        ON dbo.Stg_DDD (LOAD_RUN_ID);
END;
GO
