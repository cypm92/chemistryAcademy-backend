# Revisión visual de correos en local

Este buzón independiente genera datos ficticios y no usa la base de datos, el worker ni los volúmenes de la aplicación. Desde `backend`, con Docker Desktop en marcha:

```sh
docker compose --env-file mail-preview.env -f docker-compose.mail-preview.yml up -d ui
docker compose --env-file mail-preview.env -f docker-compose.mail-preview.yml run --rm --build examples
```

Abre http://127.0.0.1:8025 y revisa los quince mensajes: solicitudes (administración y alumna), confirmación, rechazo, reprogramación, cancelación de clase confirmada y cancelación de solicitud pendiente (para administración y alumna), contacto, registro, recordatorio e informes diario y mensual. Las clases de las notificaciones tienen una fecha fija ficticia: lunes 2 de noviembre de 2026, de 14:00 a 15:00, hora de Madrid; el horario anterior solo aparece en la reprogramación. Para el recordatorio se simula una hora previa a la clase. Los informes simulan el 1 de noviembre de 2026: dos alumnos, clases pagada y pendiente de pago en octubre y una clase anterior sin gestionar. El mensual muestra los estados en español y un resumen por alumno. Abre cada mensaje para ver HTML y texto. Repetir el segundo comando añade otra tanda sin borrar las capturas anteriores. Si cambias las plantillas, vuelve a ejecutar el segundo comando para reconstruir la imagen. El puerto 8025 debe estar libre.

Para parar únicamente este proyecto, sin borrar volúmenes de usuarios:

```sh
docker compose --env-file mail-preview.env -f docker-compose.mail-preview.yml down
```

Los mensajes se pierden cuando se recrea o elimina Mailpit. No uses información personal. Las direcciones de ejemplo usan `example.invalid`; las notificaciones se generan mediante las funciones reales y una base SQLite en memoria, sin consultar datos existentes. Mailpit y el generador están en una red Docker interna, sin relay SMTP ni puerto SMTP publicado. Un proxy HTTP expone solo la interfaz en localhost (no tiene autenticación); no publiques su puerto en otras interfaces. El proxy tiene otra red para que Docker Desktop pueda publicar el puerto, pero únicamente reenvía HTTP a Mailpit. El worker normal permanece en modo `log`; este procedimiento no captura su cola real ni envía sus mensajes.

`mail-preview.env` es deliberadamente vacío de valores y se indica explícitamente para evitar que Compose cargue el archivo `.env` del proyecto. No añadas credenciales ni cambies la configuración SMTP de este entorno para apuntar a otro servidor.
