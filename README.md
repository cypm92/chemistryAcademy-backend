# Chemistry Academy

Primera versión funcional de una academia privada para compartir vídeos y PDF con
accesos individuales y fecha de caducidad.

## Funciones incluidas

- Registro público como invitado y acceso con JWT.
- Panel de administración para crear usuarios, subir materiales y conceder acceso.
- Permisos por usuario con inicio y fin; atajos de 1 semana y 1 mes en la interfaz.
- Vídeo servido con peticiones `Range` y PDF mostrado dentro de la aplicación.
- Descarga bloqueada por defecto y habilitable por material y usuario.
- SQLite y archivos locales para desarrollo; PostgreSQL y un bucket privado para producción.

## Pruebas de backend

Las pruebas de aplicación requieren PostgreSQL aislado; no usan la base del
Compose ni aceptan una `DATABASE_URL` arbitraria. Instala las dependencias de
`requirements-test.txt`, inicia Docker y descarga la imagen antes de ejecutarlas:

```sh
docker pull postgres:16-alpine
python -m pytest tests/test_env_ignore.py tests/test_ephemeral_postgres.py -q
python tests/run_ephemeral_material_tests.py -q
python tests/run_remaining_postgres_tests.py -q
```

Los ejecutores crean servidores temporales con almacenamiento efímero y los
eliminan al terminar; el primero ejecuta los casos que necesitan Docker en el
host y puede tardar más de 50 minutos. No ejecutes la batería apuntando a una
base de aplicación o de producción. La comprobación de CI conserva su red
Docker interna creada para esa ejecución y valida la identidad del servidor de
pruebas tras conectar; las comprobaciones de rutas y direcciones privadas por
sí solas no acreditan el aislamiento de esa red.

CI reparte los archivos del ejecutor de host en dos procesos independientes,
cada uno con su propio servidor de arranque y un informe JUnit separado. Esta
paralelización aumenta el uso simultáneo de recursos; su duración en GitHub
Actions aún no se ha medido. El registro de identidad de proceso está
implementado actualmente para Windows y Linux: en otros sistemas la creación
del servidor de pruebas se rechaza por seguridad.

Los registros de propiedad sin credenciales se guardan en
`~/.cache/beciencia-pytest-containers/` (o en `BECIENCIA_PYTEST_REGISTRY`).
Si se configura `BECIENCIA_PYTEST_REGISTRY`, su ruta debe ser absoluta y estar
en almacenamiento persistente, no en un directorio temporal que se elimine: una
ruta absoluta por sí sola no garantiza durabilidad. En Windows debe incluir
unidad (por ejemplo `C:/ruta/registros`); `/tmp/registros` no es absoluta allí.
Una ruta relativa se rechaza antes de iniciar Docker. Las pruebas de interrupción
utilizan además `~/.cache/beciencia-pytest-integration/<id>/`: si no pueden
recuperar el contenedor, conservan allí su registro para inspección manual.
Si se interrumpe una ejecución, revisa el registro antes de recuperar **solo**
su contenedor con `python tests/recover_ephemeral_postgres.py <registro.json>`.
La herramienta conserva registros mientras el PID registrado esté activo o no
se pueda comprobar; incluso si el PID se ha reutilizado, no detiene el recurso.
La guarda de arranque contrasta la identidad de nacimiento. No se atribuyen ni
eliminan contenedores sin registro.
Antes de crear el contenedor se registra su token y propietario. Si la creación
se interrumpe antes de guardar el ID, la recuperación busca el nombre exacto
derivado del token y contrasta la etiqueta; si no lo encuentra o aún no está
en ejecución, conserva el registro para evitar perder la atribución.

## Arranque rápido

### Entorno local con imágenes de aplicación compiladas

Este modo ejecuta PostgreSQL, backend y frontend en Docker. El frontend se
compila con su Dockerfile y se sirve con Nginx; el backend y el worker usan la
imagen compilada. Tras modificar código hay que reconstruir las imágenes.
La simulación Microsoft usa una compilación local explícita, distinta del
artefacto de producción; no emplea cuentas ni servicios reales.

La primera vez, desde la carpeta `backend`:

```powershell
Copy-Item .env.local.example .env.local
docker compose --env-file .env.local -f docker-compose.local.yml up -d --build
```

Abre `http://127.0.0.1:5174`; la API se accede mediante `/api` en ese mismo origen.
El simulador Microsoft ficticio está en `http://127.0.0.1:5174/fake/`;
consulta `local-microsoft-sandbox/RUN.md` antes de usarlo. No uses cuentas ni
secretos reales. El worker utiliza SMTP hacia Mailpit, accesible en `http://127.0.0.1:8025`.
Mailpit no publica SMTP ni tiene red externa; su buzón no es persistente entre
recreaciones. El backend envía nuevas notificaciones administrativas a
`preview@example.com` únicamente en este Compose. El planificador del worker
sigue activo: antes de reiniciarlo, revisa la cola y los informes y recordatorios
vencidos, porque puede generar mensajes adicionales con datos existentes. Sus
informes conservan el destinatario administrativo predeterminado, aunque Mailpit
no los entrega a Internet. Para ver sus logs:

```powershell
docker compose --env-file .env.local -f docker-compose.local.yml logs -f notification-worker
```

El usuario inicial es el definido en `.env.local`; con el ejemplo:

```text
Email: admin@beciencia.academy
Contraseña: ChangeMe123!
```

Para los siguientes arranques no hace falta reconstruir nada:

```powershell
# Desde backend
docker compose --env-file .env.local -f docker-compose.local.yml up -d
```

Para detenerlo:

```powershell
docker compose --env-file .env.local -f docker-compose.local.yml down
```

Los datos locales se conservan al parar los contenedores. Si quieres borrar
solo el entorno local y empezar desde cero, usa `down -v` con el mismo comando.
Nunca uses esa opción contra producción.

#### Sincronización de Teams (opt-in)

El worker consulta cada 60 segundos. Primero genera informes y recordatorios y
procesa el correo; después procesa como máximo cinco trabajos de Teams por ciclo.
Fuera de la simulación local descrita en `local-microsoft-sandbox/RUN.md`,
`MICROSOFT_SYNC_ENABLED` está desactivado por omisión. El valor explícito `true`
lo habilita únicamente si la configuración Microsoft es válida y completa
(`MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`,
`MICROSOFT_REDIRECT_URI` y `MICROSOFT_FERNET_KEY`). En el Compose local se usa
un proveedor ficticio; en un entorno real, habilitarlo permite llamadas reales.
La cuenta debe conectarse antes de procesar trabajos. Un fallo de sincronización
no impide procesar el correo, y los access tokens no se guardan.

Las reuniones ya sincronizadas no se comprueban periódicamente en Outlook.
Se mantienen los reintentos de trabajos pendientes y las comprobaciones
puntuales solicitadas para resolver errores. Una operación remota tardía tras
un fallo del worker podría dejar una discrepancia que no se detectará sola.

Si Microsoft no confirma si creó un evento, la clase muestra «Necesita revisión»
y la sincronización no repite automáticamente la creación. La administradora
debe revisar primero Outlook: si comprueba que no existe, puede confirmar un
nuevo intento desde la clase. La operación anterior podría terminar después de
esa comprobación y producir un duplicado; las incidencias se conservan para su
revisión y no se borran eventos huérfanos automáticamente. Si la identidad de la
cuenta no coincide, se requiere revisión y no se autoriza la recreación. No se
crean reuniones automáticamente para clases confirmadas antes de conectar la
cuenta. Una operación ya iniciada puede terminar después de desconectar, y la
desconexión no revoca por sí misma los permisos concedidos en Microsoft.

#### Cargar datos ficticios para probar la plataforma

Con el entorno local arrancado, este comando crea una colección reutilizable de
alumnos, un segundo administrador, árbol de carpetas, PDFs de ejemplo, etiquetas,
permisos, reservas, bloqueos y solicitudes de la portada:

```powershell
docker compose --env-file .env.local -f docker-compose.local.yml exec backend python -m app.seed_demo
```

Se puede ejecutar más de una vez: actualiza los datos demo y vuelve a crear las
reservas y solicitudes ficticias. Nunca se ejecuta automáticamente ni está
conectado a producción.

Credenciales útiles tras sembrar:

```text
Alumna: alba@demo.beciencia.academy / AlumnoDemo123!
Administradora: marta@demo.beciencia.academy / DemoAdmin123!
```

### Opción recomendada: Docker

Sólo necesitas Docker Desktop. Ejecuta Compose desde la carpeta `backend`:

```powershell
docker compose up --build
```

Cuando termine de arrancar, abre:

- Aplicación: `http://localhost:8081`
- Documentación del backend: `http://localhost:8001/docs`
- Estado del backend: `http://localhost:8001/health`

El administrador local inicial es:

```text
Email: admin@chemistry-academy.com
Contraseña: ChangeMe123!
```

Los vídeos y PDF se conservan en el volumen `chemistry_files`, y PostgreSQL en
`chemistry_db`. Por tanto, detener los contenedores no borra los datos.

Para detener la aplicación:

```powershell
docker compose down
```

Los puertos se han elegido para que Chemistry Academy pueda ejecutarse al mismo
tiempo que LifeHub. Si necesitas cambiarlos, puedes definirlos antes de arrancar:

```powershell
$env:CHEMISTRY_FRONTEND_PORT = "8090"
$env:CHEMISTRY_BACKEND_PORT = "8010"
docker compose up -d --build
```

Para ver los registros:

```powershell
docker compose logs -f
```

`docker compose down -v` también elimina la base de datos y todos los archivos
subidos. Úsalo únicamente si quieres reiniciar completamente el entorno local.

### Compartir una demo temporal por Internet

Cloudflare Quick Tunnel permite compartir la aplicación sin abrir puertos del
router ni contratar un dominio. Está pensado exclusivamente para pruebas.

Desde `backend`, arranca la aplicación y el túnel:

```powershell
docker compose --profile demo up -d --build
docker compose logs -f tunnel
```

En los logs aparecerá una URL similar a
`https://nombre-aleatorio.trycloudflare.com`. Comparte únicamente esa URL.
El portátil y Docker deben permanecer encendidos.

Para consultar la URL actual sin revisar los logs, ejecuta:

```powershell
.\Get-PublicUrl.ps1
```

Y para abrirla directamente en el navegador:

```powershell
.\Get-PublicUrl.ps1 -Open
```

La URL no se guarda automáticamente en el README: Cloudflare la cambia al
reiniciar el túnel y hacerlo generaría modificaciones de Git innecesarias.

Para apagar la demo:

```powershell
docker compose --profile demo down
```

La URL deja de funcionar inmediatamente y será distinta en el siguiente
arranque.

### Opción manual desde VS Code

Requisitos: Python 3.11+ y Node 20+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload --log-config app/uvicorn-log.json
```

**Antes de activar Microsoft OAuth o desplegar:** usa siempre la configuración
`app/uvicorn-log.json` (también en comandos personalizados y workers de Uvicorn).
Redacta la consulta del callback únicamente en los access logs estándar de
Uvicorn; Nginx desactiva su access log para esa ruta exacta. No protege error
logs, otros formatos de logging, trazas, APM ni proxies/collectors adicionales.
Aunque la configuración actual de Caddy no habilita access logs, antes del
despliegue hay que revisar y proteger Caddy y cada proxy, CDN, balanceador,
colector y herramienta de observabilidad para que nunca registren ni capturen
la query completa del callback. Verifica también configuraciones personalizadas.

En otra terminal, desde la carpeta hermana `frontend`:

```powershell
npm install
npm run dev
```

Abre `http://localhost:5173`. El administrador inicial se crea con los valores
de `.env` (`admin@chemistry-academy.com` / `ChangeMe123!` por defecto). Cámbialos antes
de publicar la aplicación.

La documentación interactiva del API queda en `http://127.0.0.1:8000/docs`.

## Producción y almacenamiento

Para una primera etapa económica, la opción recomendada es **Cloudflare R2**:
bucket privado, sin enlaces públicos y normalmente sin coste de salida hacia
Internet. El backend debe emitir acceso temporal o actuar como proxy, nunca
guardar una URL pública. Como alternativa, Backblaze B2 es barato y sencillo.

Los PDFs y vídeos visibles en un navegador no pueden hacerse técnicamente
imposibles de copiar: una persona siempre puede grabar la pantalla. Esta versión
evita la descarga casual y el acceso sin permiso. Para elevar la protección se
recomienda, por orden: marca de agua dinámica con email, HLS segmentado con URLs
firmadas y, sólo si el negocio lo justifica, un proveedor DRM.

Antes de producción:

1. Cambiar `SECRET_KEY` y credenciales iniciales.
2. Usar PostgreSQL mediante `DATABASE_URL`.
3. Sustituir el adaptador local por R2 privado.
4. Servir todo bajo HTTPS y limitar `CORS_ORIGINS` al dominio real.
5. Añadir copias de seguridad, límites de subida, antivirus y recuperación de contraseña.
