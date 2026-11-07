# Ensayo Microsoft real local (preparado, NO activado)

No se ha creado ninguna base, credencial ni conexión. El simulador conserva su configuración y sus datos. Este perfil usa el mismo proyecto Compose, PostgreSQL y puerto, pero apunta exclusivamente a la base distinta `beciencia_microsoft_real_local` y monta un volumen distinto `beciencia_microsoft_real_local_files` en `/app/storage`; el volumen del simulador permanece intacto. No copiar ni migrar allí datos o trabajos del simulador. Antes de cualquier activación, solicitar autorización explícita y confirmar que la base nueva no existe o está vacía; crearla entonces en el PostgreSQL existente, sin borrar ni modificar `beciencia_local`. La aplicación inicializará su esquema en la nueva base.

Tras esa autorización, configurar fuera del chat un archivo privado ignorado `.env.microsoft-real.local` con los nombres `LOCAL_SECRET_KEY`, `LOCAL_ADMIN_NAME`, `LOCAL_ADMIN_EMAIL`, `LOCAL_ADMIN_PASSWORD`, `REAL_MICROSOFT_TENANT_ID`, `REAL_MICROSOFT_CLIENT_ID`, `REAL_MICROSOFT_CLIENT_SECRET` y `REAL_MICROSOFT_FERNET_KEY`. No guardar ejemplos con valores reales en el repositorio ni mostrar `docker compose config` con valores. Configurar en el registro de aplicación la URI exacta `http://localhost:5174/api/integrations/microsoft/callback`. Los valores vacíos y los valores conocidos del simulador fallan; esto no puede reconocer todos los posibles marcadores ficticios ni verificar credenciales.

Solo después de aprobación y creación de la base, usar conjuntamente `docker-compose.local.yml` y `docker-compose.microsoft-real.local.yml` con `--env-file .env.microsoft-real.local`. Abrir exclusivamente `http://localhost:5174/`: la raíz de 127.0.0.1 redirige a localhost; otras rutas en ese host se rechazan para evitar mezclar cookies. La profesora iniciará sesión personalmente. No ejecutar `down -v`. Para volver al simulador se requiere recrear los servicios con solo el Compose base y su archivo de entorno local, sin borrar volúmenes.

## Incorporación de clases existentes

Tras conectar Microsoft, las nuevas confirmaciones de solicitudes (incluso si se solicitaron antes de la conexión) y las nuevas clases creadas por administración generan reuniones automáticamente. Las clases ya confirmadas sin trabajo de Teams se gestionan manualmente: reprogramarlas o cambiar su duración o asignatura no crea una reunión. Las clases que ya tienen trabajo de Teams siguen sincronizándose.

**Límites importantes:** el procesamiento Microsoft está desactivado, pero las acciones sobre reservas pueden seguir encolando trabajos mientras está apagado; conectar una cuenta no autoriza crear eventos. Inmediatamente antes de activar el procesamiento se requiere autorización posterior y contar y revisar los trabajos pendientes y el estado de las conexiones en la base nueva, sin mostrar datos sensibles. El correo del worker sigue en Mailpit local, sin credenciales SMTP. El overlay da salida de red general, no filtrada por dominio, al backend y al worker: Docker no impone una lista de destinos Microsoft. El navegador también contactará Microsoft al iniciar sesión. La base y el almacenamiento siguen en volúmenes locales; no usar credenciales ni datos de producción. El gateway no debe registrar secretos ni consultas; el filtro Uvicorn redacta la consulta del callback. No habilitar logs de depuración HTTP ni volcar cabeceras, cookies, configuración o tokens.

Las revisiones PATCH antiguas no se migran automáticamente: la tabla histórica
`teams_patch_review_decisions` se conserva intacta pero queda inactiva. Las
operaciones con resultado desconocido requieren una nueva revisión explícita.
Detén los workers anteriores antes de actualizar, como en cualquier migración
Teams, para evitar respuestas simultáneas de versiones antiguas.

### Autorizaciones antiguas de recreación

La tabla histórica `teams_recreation_decisions`, si existe, se conserva sin borrar datos. Sus decisiones no autorizan nuevos envíos. Si falta la identidad verificable del intento histórico, el endpoint rechaza la recreación y el caso requiere soporte manual; no se infiere esa identidad ni se promete una revisión ordinaria. La autorización nueva se guarda en el trabajo, vinculada a la transacción y cuenta originales; el intento solo se crea al preparar el POST. Si cambia la cuenta, reconecta la original y usa Reintentar. Detener los workers antiguos antes de migrar.
