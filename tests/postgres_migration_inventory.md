# Inventario preliminar de migración a PostgreSQL

Estado: **incompleto; no autoriza retirar pruebas ni ejecutar limpieza**. Tarjeta `t_5fbc7298`. Fuente: búsqueda `rg -il sqlite tests -g '*.py'` y revisión parcial de fixtures y nombres de pruebas. La lista cubre los 42 archivos encontrados, pero no constituye una clasificación completa caso por caso: antes de retirar cualquier parámetro o prueba hay que leer sus aserciones y dependencias. No se ha migrado ningún test.

Leyenda: **F** conservar cobertura funcional y migrar fixture; **M** migraciones/esquema, preservar datos heredados y sus aserciones; **P** contiene también PostgreSQL; **C** concurrencia, necesita conexiones independientes; **H** cliente HTTP/override y arranque global; **A** almacenamiento temporal. Las marcas describen el trabajo a investigar, no afirman que cada caso del archivo use la misma infraestructura.

| Archivo en tests/ | Clasificación y dependencias principales |
|---|---|
| test_admin_bootstrap.py | F; motor en memoria, creación de metadatos, persistencia de contraseña al reiniciar. |
| test_admin_reports.py | F, M; informes, transacciones y migración de worker; también hay casos sin base. |
| test_authorization.py | F, H; StaticPool, cliente y override, permisos de materiales. |
| test_booking_policy.py | F, H; StaticPool, API y reglas horarias/transacciones. |
| test_class_management.py | F, M, H; gestión de clases y migración heredada; contiene prueba simulada del dialecto PostgreSQL. |
| test_guest_booking_flow.py | F, H; StaticPool y flujo de reservas de invitado. |
| test_legacy_email_responses.py | F, H; StaticPool y respuestas de correo heredadas. |
| test_material_content_disposition_unicode.py | F, H, A; StaticPool, cliente y archivos temporales. |
| test_material_metadata.py | F, P, A; fixture opt-in PostgreSQL o SQLite, permisos y transacciones. Cambio preexistente sin seguimiento; no alterar. La protección PostgreSQL solo comprueba nombre de base. |
| test_material_move.py | F, H; StaticPool, API y conservación de archivo y permisos. |
| test_material_unicode_permissions.py | F, H, A; StaticPool, acceso y almacenamiento temporal. |
| test_microsoft_auth.py | F; StaticPool, subconjuntos de tablas OAuth y credenciales. |
| test_microsoft_end_to_end.py | F, H, A; SQLite en archivo temporal, aplicación completa; comparar cobertura con contraparte PostgreSQL sin presumir equivalencia. |
| test_microsoft_integration.py | F, M, H, P; URL PostgreSQL opcional, fallback SQLite, esquema heredado y arranque de aplicación. |
| test_microsoft_oauth_routes.py | F, H, P, C; StaticPool, fallback en caso de callback, carrera PostgreSQL en esquema aislado. |
| test_microsoft_refresh.py | F, P, C; SQLite temporal/subconjunto de tablas y carreras PostgreSQL con URL opt-in. |
| test_microsoft_refresh_identity.py | F, P, C; SQLite temporal y carreras de identidad PostgreSQL opt-in. |
| test_microsoft_schema_upgrade.py | M, P, C; fixture parametrizado y esquemas PostgreSQL, inicialización concurrente; caso de una conexión no debe descartarse sin sustitución funcional. |
| test_microsoft_worker_startup.py | F, M; SQLite temporal, inicio de worker y actualización de esquema heredado. |
| test_notifications.py | F, M, H; StaticPool y motor adicional heredado para migraciones. |
| test_public_resources.py | F, M, H; StaticPool y motor heredado adicional. |
| test_registration_notification.py | F, H; StaticPool, registro y notificaciones. |
| test_teams_ambiguous_creation.py | F; SQLite en memoria y resultado ambiguo de creación. |
| test_teams_api.py | F, H; StaticPool, endpoints Teams y metadatos. |
| test_teams_attempt_worker_identity.py | F; motores SQLite por caso, identidad de trabajador y metadatos. |
| test_teams_booking_hooks.py | F, H; StaticPool y hooks de reserva. |
| test_teams_creation_attempt_schema.py | M, P; esquemas y restricciones, fixture parametrizado y base PostgreSQL opt-in. |
| test_teams_delete_hooks.py | F, P, C; hooks y carreras PostgreSQL; revisar por separado el alcance de limpieza de cada motor. |
| test_teams_event_ownership_concurrency.py | M, P, C; parametrizado, SQLite en archivo, esquema PostgreSQL y conexiones concurrentes. |
| test_teams_legacy_orphan_migration.py | M; migración de huérfanos y revisión manual sin mutación. |
| test_teams_legacy_readonly.py | M; SQLite temporal y esquema heredado de solo lectura. |
| test_teams_orphan_no_auto_delete.py | F, M; motores SQLite y seguridad de huérfanos. |
| test_teams_patch_review_decision_schema.py | M, P, C; parametrizado, esquema y concurrencia. |
| test_teams_recreation_account_binding.py | F, M; autorización ligada a cuenta y migración heredada. |
| test_teams_recreation_integrated_schema.py | M; tablas de autorización heredada e integrada. |
| test_teams_recreation_worker_authorization.py | F; SQLite y autorización de worker. |
| test_teams_remote_operation_schema.py | M, P; fixture parametrizado y esquema PostgreSQL. |
| test_teams_sync.py | F; varios motores SQLite, sincronización y metadatos. |
| test_teams_tombstone.py | F, P; fixture parametrizado y esquema PostgreSQL, preservar comportamiento. |
| test_teams_unknown_safety.py | F, M, P, C; migración y rechazo seguro, casos separados SQLite y PostgreSQL. |
| test_transactional_copy.py | F; varios motores SQLite, copia y transacciones. |
| test_unicode_character_classes.py | F, H, A; StaticPool, cliente y archivos temporales. |

## Casos específicos de SQLite y cobertura mixta

`test_teams_unknown_safety.py::test_sqlite_legacy_migration_is_idempotent` identifica explícitamente SQLite; existe además un caso PostgreSQL homólogo, pero aún no se ha comparado su cobertura para decidir si basta como sustituto. No se ha demostrado que un archivo completo sea exclusivamente SQLite. La parametrización SQLite de los archivos mixtos no autoriza borrar las aserciones funcionales compartidas. Hay que revisar cada test, registrar las retiradas y conservar las pruebas PostgreSQL y de concurrencia.

## Riesgos y dependencias transversales

* `app/database.py` construye el motor global al importar y ejecuta inicialización de esquema; sustituir la dependencia por fixture no basta para aislar el arranque. Configurar su destino antes de importar y verificar ambos motores.
* La limpieza destructiva de fixtures existentes y la creación de esquemas exigen validar servidor y aislamiento, no solo nombre. `test_material_metadata.py` no proporciona esta garantía. No ejecutar los fixtures destructivos contra bases ajenas.
* Tests concurrentes requieren varias conexiones reales al mismo esquema y no quedan aislados por una transacción de una sola conexión.
* `.github/workflows/ci.yml:37-101` separa test host y contenedor PostgreSQL, prepara varias bases y rechaza omisiones no permitidas. Actualmente la batería aún contiene motores SQLite y fallback; disponer de URLs PostgreSQL en CI no demuestra migración completa.
* Faltan inventario de DDL y conexiones por caso, clasificación final de pruebas exclusivas, interfaz compartida, pruebas de rechazo y aislamiento PostgreSQL real, limpieza verificada y revisión independiente.

## Bloqueo de ejecución

El terminal headless rechazó la limpieza SQL que requiere aprobación interactiva. No eludir esa barrera ni crear bases que no se puedan limpiar. Debe habilitarse una aprobación explícita para la operación acotada o realizar su ciclo en un contexto interactivo autorizado. Este documento no propone ejecutar comandos destructivos.
