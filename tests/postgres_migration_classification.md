# Clasificación e interfaz propuesta para pruebas PostgreSQL

Estado: inventario histórico y diseño inicial, conservados como referencia; el enfoque posterior usa PostgreSQL efímero por prueba. La ejecución integral y la revisión independiente siguen pendientes. Tarjeta `t_fca05397`; complementa `tests/postgres_migration_inventory.md`. Alcance: archivos Python bajo `tests` que tenían referencias textuales a SQLite, no todos los tests que usan base de datos. Las líneas describen el árbol de trabajo anterior a la migración y pueden haber cambiado. DEC-001 distingue producción PostgreSQL y desarrollo SQLite; este trabajo migra las pruebas. No se autoriza retirar un test por aparecer en esta tabla.

## Leyenda

**S** motor SQLite; **F** fixture/motor/sesión local; **D** DDL o migración explícita; **H** cliente HTTP, overrides o arranque; **P** ruta PostgreSQL existente; **C** concurrencia/conexiones múltiples; **A** almacenamiento temporal; **X** caso o parámetro específicamente SQLite, no necesariamente todo el archivo. `metadata` significa `Base.metadata.create_all`; no implica que sea la única operación DDL. Las referencias son puntos de entrada para revisar cada archivo, no una afirmación de que todas sus pruebas tengan el mismo ciclo de vida. Las pruebas funcionales y sus aserciones se preservan aunque se retire un motor.

## Inventario por archivo (rutas relativas a `tests/`)

| Archivo | Categorías | Dependencias y evidencia para la migración |
|---|---|---|
| test_admin_bootstrap.py | S F D | Motor memoria, metadata y sessionmaker; persistencia de admin: 16-18. |
| test_admin_reports.py | S F D H | Motor memoria/metadata: 24-26; worker con inicialización simulada: 170, 229; conservar informes y casos sin DB. |
| test_authorization.py | S F D H | StaticPool, metadata y override get_db: 33-59; permisos HTTP. |
| test_booking_policy.py | S F D H | StaticPool y sesiones; cliente y override: 25-39; reservas y transacciones. |
| test_class_management.py | S F D H | Motor y cliente: 18-33; CREATE heredado bookings: 202-204; también simulación de dialecto PostgreSQL. |
| test_guest_booking_flow.py | S F D H | StaticPool, metadata, get_db y cliente: 17-34. |
| test_legacy_email_responses.py | S F D H | StaticPool, metadata y cliente: 17-39; respuestas heredadas. |
| test_material_content_disposition_unicode.py | S F D H A | Motor y metadata 18-23; override 36-42; archivos temporales y cabeceras. |
| test_material_metadata.py | S F D H P A | URL PostgreSQL opt-in o SQLite: 19-28; drop_all antes y después: 25,60; get_db: 54-58. Archivo preexistente no versionado: conservarlo; validar servidor además del nombre antes de cualquier DDL. |
| test_material_move.py | S F D H | Motor y metadata: 17-23; cliente/override: 29-30,61; conservar permisos y archivo. |
| test_material_unicode_permissions.py | S F D H A | Motor, metadata y cliente: 19-36; permisos y almacenamiento. |
| test_microsoft_auth.py | S F D | Tres motores StaticPool y subconjuntos de tablas: 13-15,34-36,49-51; no asumir metadata completa. |
| test_microsoft_end_to_end.py | S F D H A | Archivo temporal 24; metadata 40; overrides de DB y factory 82-85, restauración 107-108; inicialización simulada 89. Comparar con e2e PostgreSQL antes de sustituir. |
| test_microsoft_integration.py | S F D H P | URL opcional con fallback SQLite 48-49; CREATE legacy_marker 52 y metadata 54; overrides DB/factory 74-76; preservar arranque y datos heredados. |
| test_microsoft_oauth_routes.py | S F D H P C | Tablas parciales 57-59; fallback 174-175; esquema PostgreSQL con factory y limpieza 224-246,294; carreras y mocks OAuth. |
| test_microsoft_refresh.py | S F D P C | Motores SQLite archivo/memoria y tabla parcial 17-32; casos PostgreSQL a partir de 121; preservar carreras y sesiones independientes. |
| test_microsoft_refresh_identity.py | S F D P C | SQLite archivo y dos tablas 22-24; PostgreSQL separado 65-68; identidad y concurrencia. |
| test_microsoft_schema_upgrade.py | S F D P C X | Fixture parametrizado 13-30; esquema PG y limpieza 29-37; DDL heredado 43-45,86-90; pool SQLite de una conexión 128-131; concurrencia PG 149-183. Mantener cobertura de migración y arranque. |
| test_microsoft_worker_startup.py | S F D H | Worker simulado 16; SQLite temporal y CREATE legacy 33-38; segundo motor 132-144; conservar orden de arranque. |
| test_notifications.py | S F D H | Motor y HTTP 20-39; motor adicional y CREATE heredado notification_events 61-63. |
| test_public_resources.py | S F D H | Motor/cliente 16-36; CREATE heredado public_resources 119-122; conservar migración y visibilidad. |
| test_registration_notification.py | S F D H | StaticPool, metadata, get_db y cliente: 16-30; registro y notificación. |
| test_teams_ambiguous_creation.py | S F D | Motor/metadata/sesión 18-20; seguridad de resultado incierto. |
| test_teams_api.py | S F D H | Motor/metadata y get_db: 19-36; endpoints Teams. |
| test_teams_attempt_worker_identity.py | S F D | Motores y sesiones por caso: 16-18,38-40,57-59,74-76; identidad worker. |
| test_teams_booking_hooks.py | S F D H | Dos motores, clientes y overrides: 19-47,53-70; hooks de reservas. |
| test_teams_creation_attempt_schema.py | S F D P X | Inicialización y ALTER 13-34; fixture parametrizado y limpieza de esquema PG 39-61; CREATE jobs y migración 85-118. |
| test_teams_delete_hooks.py | S F D H P C X | PRAGMA FK y cliente 18-38; prueba reutilización de ID SQLite 96; caso PostgreSQL con metadata y drop_all 220-283; proteger servidor. |
| test_teams_event_ownership_concurrency.py | S F D P C X | Parametrización 16-29, limpieza PG 36, CREATE jobs/arranque 42-45; conexiones concurrentes. |
| test_teams_legacy_orphan_migration.py | S F D | Metadata y CREATE tabla heredada 11-16,34-39; ensure_application_schema 25,45; conservar bloqueo y datos. |
| test_teams_legacy_readonly.py | S F D X | CREATE tabla y PRAGMA de inspección 14-21,30; inicialización 24-27. Sustituir inspección específica por PostgreSQL preservando inmutabilidad y errores. |
| test_teams_orphan_no_auto_delete.py | S F D | Metadata y CREATE tabla heredada 14-18; segundo motor 35-37; prohibición de borrado automático. |
| test_teams_patch_review_decision_schema.py | S F D P C X | Fixture parametrizado/esquema 16-38; CREATE operaciones y decisiones 54-59; migración y concurrencia. |
| test_teams_recreation_account_binding.py | S F D X | Motor, CREATE y llamada explícita al dialecto sqlite 56-63; preservar identidad y aserciones de migración. |
| test_teams_recreation_integrated_schema.py | S F D | Metadata 8-9; CREATE dos tablas heredadas y arranque 20-27. |
| test_teams_recreation_worker_authorization.py | S F D | Motor, metadata, sesiones 21-23; autorización worker. |
| test_teams_remote_operation_schema.py | S F D P X | Fixture parametrizado y esquema 13-35; CREATE jobs y arranque 41-44. |
| test_teams_sync.py | S F D | Cinco motores y metadata independientes 15-17,42-44,72-74,98-100,123-125; sincronización. |
| test_teams_tombstone.py | S F D P X | Fixture por dialecto 16-30; esquema PG y limpieza 23-38; preservar tombstone. |
| test_teams_unknown_safety.py | S F D P C X | SQLite/metadata 31-33,63-65; idempotencia específica 85-91 y PG 111-128; carrera PG 136-174; sqlite_master 179-185; preservar comportamiento de fallo seguro. |
| test_transactional_copy.py | S F D | Motores y metadata 27-29,45-47,76-78; transacciones/copia, no solo sintaxis SQLite. |
| test_unicode_character_classes.py | S F D H A | Motor, metadata y cliente 30-57; acceso Unicode y ficheros. |

## Exclusividad y decisiones de retirada

### Actualización de migración — verificación pendiente

El inventario siguiente es histórico; la solución de esquema compartido descrita
debajo fue sustituida por contenedores PostgreSQL efímeros por caso, a petición
del usuario. Se retiraron las variantes parametrizadas SQLite, manteniendo los
casos funcionales en PostgreSQL; esto no demuestra por sí mismo que toda la
batería haya pasado. El caso de reutilización automática de ID de SQLite de
`test_teams_delete_hooks.py` pasó a forzar la reutilización del ID en PostgreSQL,
conservando la comprobación de separación de trabajos. El caso de migración
legacy de SQLite de `test_teams_unknown_safety.py` se ejecuta ahora en PostgreSQL;
se conserva también la prueba PostgreSQL existente. Las pruebas del esquema
Microsoft con una sola conexión conservan el límite de pool en PostgreSQL.
`test_postgres_target_guard.py` menciona SQLite deliberadamente para probar que
se rechaza; no abre una base SQLite. Quedan pendientes la ejecución completa,
la contabilización de omisiones y la revisión independiente.

Ninguno de estos archivos queda clasificado completo como exclusivamente SQLite. Candidatos por caso: `test_teams_unknown_safety.py::test_sqlite_legacy_migration_is_idempotent` (85), `test_teams_delete_hooks.py::test_physical_delete_detaches_job_before_sqlite_reuses_booking_id` (96), y el caso de pool SQLite de una conexión `test_microsoft_schema_upgrade.py:128`; revisar sus aserciones, parametrización y equivalentes antes de retirar. `PRAGMA`, `sqlite_master`, argumento dialecto `sqlite` y parámetros SQLite son piezas específicas del motor; pueden coexistir con garantías funcionales que deben trasladarse. Lista de candidaturas, no decisión de borrar ni demostración de equivalencia. Comparar también `test_teams_legacy_readonly.py` con un test PostgreSQL que pruebe explícitamente preservación sin mutación; no borrar solo por usar PRAGMA. La clasificación individual no es una auditoría exhaustiva de cada aserción o test indirecto.

## Interfaz propuesta inicialmente (histórica, sustituida)

Contrato propuesto, **aún no existe**; la tarjeta `t_49aafafb` debe implementarlo y verificarlo. Módulo `tests/postgres_test_support.py`:

```python
@dataclass(frozen=True)
class PostgresTestDatabase:
    engine: sqlalchemy.engine.Engine
    session_factory: sqlalchemy.orm.sessionmaker
    schema: str

@pytest.fixture(scope="function")
def postgres_test_db() -> Iterator[PostgresTestDatabase]: ...
```

* Cada invocación obtiene esquema exclusivo en la base dedicada de pruebas; `engine` abre conexiones PostgreSQL independientes con `search_path` restringido a ese esquema; `session_factory()` retorna sesiones independientes. No usar rollback de una sola conexión como aislamiento de las carreras. El fixture entrega un esquema **vacío**; cada test decide entre `Base.metadata.create_all(db.engine)`, subconjunto de tablas o DDL heredado antes de llamar la migración. Para clases unittest y tests actuales sin inyección pytest, acceso mediante fixture adaptadora explícita; no compartir esquema entre casos salvo necesidad documentada.
* Entrada de configuración propuesta: `PYTEST_POSTGRES_URL`, obligatoria, sin fallback ni saltos silenciosos; no imprimirla. Restricción local: servicio Compose `beciencia-local`/`db` y base dedicada `beciencia_pytest_disposable`, sin inferir seguridad solo del nombre. Antes de DDL verificar identidad del servicio/servidor efectivo, base, usuario y entorno autorizado; si falta alguna comprobación, abortar antes de cualquier escritura. CI necesita una política independiente explícitamente autorizada, no habilitar cualquier host. No codificar credenciales en fuentes o salida.
* Un esquema de nombre generado y acotado por ejecución/caso debe comprobarse inexistente antes de crearse. Registrar propiedad de recursos de esta ejecución, cerrar clientes, sesiones y conexiones antes de limpiar y verificar la desaparición solo de recursos propios. Si el recurso existía, la identidad cambia, o la operación de limpieza no tiene aprobación disponible, fallar sin borrarlo; no crear recursos si no puede completarse el ciclo aprobado. La validación real de identidad Compose/CI y de propiedad debe concretarse y probarse en implementación: nombre o URL solos no bastan.
* Antes de importar `app.database` o `app.main`, el ejecutor configura explícitamente `DATABASE_URL` para que el motor global apunte a un destino PostgreSQL aislado autorizado; también destinos temporales de almacenamiento fuera del repo y sincronización/worker desactivados. `app/database.py:11-13` crea el motor global al importar; `ensure_application_schema` y funciones con parámetros por defecto capturan ese motor (`app/database.py:16,158-174,177-186`). Un override de `get_db` **no** cambia el motor global ni los defaults capturados. Debe probarse por separado el motor global y el fixture, incluyendo lifespan/TestClient, y evitar colisiones de esquemas entre ambos.
* Integración FastAPI: adaptador de test configura y restaura `app.dependency_overrides[get_db]` con `db.session_factory()` y, donde se usa, `get_session_factory`; conserva mocks de transporte, limpia overrides incluso en excepción y cierra cliente antes de destruir esquema. No ocultar con mocks una prueba cuyo objetivo sea el arranque real.
* Verificaciones obligatorias de la implementación: destinos no PostgreSQL, host/servidor/base/usuario no autorizados, base o esquema preexistente, fallo parcial y limpieza únicamente propia; aislamiento de dos fixtures y visibilidad entre dos conexiones reales; DDL heredado, conexiones concurrentes y arranque global/test separados; capturar ausencia de secretos en errores. No hay evidencia de que estos tests ya existan o pasen.

## Dependencias transversales y CI

`app/database.py:11-13,158-174` y los hooks de `app/main.py`/worker condicionan el arranque, además de las conexiones en cada fila. Los casos existentes puramente PostgreSQL fuera de las 42 filas también necesitan inventario al migrar toda la batería: por ejemplo `test_postgres_real_booking_races.py`, `test_teams_postgres.py`, `test_admin_reports_postgres.py` y `test_microsoft_end_to_end_postgres.py`; este conjunto no pretende contarlos.

`.github/workflows/ci.yml:37-38` ejecuta test host, `:39-83` ejecuta batería en contenedor PostgreSQL con varias URLs y `:85-101` rechaza omisiones salvo una excepción concreta. Las URLs de CI no demuestran que el motor global ni cada prueba usen PostgreSQL; habrá que adaptar configuración de arranque y fixtures, conservar ejecución host y puerta de omisiones y contabilizar suite completa. No se modifica CI en esta fase. No se ha ejecutado ninguna prueba ni DDL y no se ha verificado limpieza: el terminal headless exige aprobación interactiva para la operación SQL de limpieza. No eludirla.
