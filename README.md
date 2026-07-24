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

## Arranque rápido

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
uvicorn app.main:app --reload
```

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
