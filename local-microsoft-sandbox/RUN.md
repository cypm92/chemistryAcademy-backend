# Simulación Microsoft local integrada

## Compose local habitual (5174)

Desde `backend`, ejecuta `docker compose --env-file .env.local -f docker-compose.local.yml up -d`. Abre `http://127.0.0.1:5174/` y el simulador en `http://127.0.0.1:5174/fake/`. Inicia sesión con la cuenta administradora local ya configurada en `.env.local`; no son las credenciales del sandbox antiguo. El callback OAuth utiliza la dirección `127.0.0.1`, no `localhost`. El puerto 8001 ya no se publica. La base de datos y archivos existentes se conservan; el simulador mantiene eventos y tokens en memoria, que se pierden si se reinicia. No uses datos ni cuentas reales.

Backend, worker, base de datos, Vite y simulador están en una red interna sin salida; el gateway también pertenece a una red de entrada para publicar exclusivamente en loopback. Mailpit captura el correo local; su interfaz está en http://127.0.0.1:8025/ (consulta docker-compose.local.yml). Vite usa dependencias ya instaladas en el volumen local `node_modules`; si faltan, deben instalarse antes de arrancar en ese volumen. No uses `down -v`.

## Prueba y limitaciones

### Revisión visual: creación ambigua y recreación autorizada

1. Abra BeCiencia y Outlook ficticio en las direcciones anteriores. Conecte la cuenta ficticia y mantenga ambas pestañas abiertas. Use únicamente reservas y datos de prueba.
2. En Outlook ficticio elija **Creación ambigua: evento SÍ creado, respuesta 503** y pulse **Aplicar escenario**. Cree o confirme una reserva que necesite reunión y espere al worker. La próxima creación simulada guardará el evento, pero devolverá 503. Refresque Outlook y compruebe el evento. No autorice una recreación mientras exista: la revisión simplificada no vincula eventos ya existentes.
3. Para probar la ausencia, en una nueva reserva elija **Creación ambigua: evento NO creado, respuesta 503** antes de crearla o confirmarla. Espere al worker y compruebe en Outlook ficticio que no aparece. En la revisión de BeCiencia compruebe la ausencia y, solo entonces, autorice explícitamente la recreación. Espere al worker y refresque Outlook para comprobar el resultado.
4. Alternativamente, para simular que se eliminó un evento guardado, identifique por título y hora el evento concreto en Outlook ficticio, pulse **Eliminar evento ficticio** en esa fila y confirme. Solo se elimina ese evento de la memoria del simulador; no se borra ninguna reserva ni evento real. Refresque y verifique antes de autorizar una recreación en BeCiencia.

Los dos escenarios ambiguos se consumen una sola vez por la próxima creación, no por consultas intermedias. Para cancelarlos elija **Normal** y aplique. 404 y 429 afectan a la próxima operación. El selector muestra el escenario pendiente. El simulador y su gateway deben cargar estos cambios cuando se inicien de nuevo; reiniciarlos borra los eventos/tokens ficticios en memoria, por lo que no los reinicie durante otra prueba activa.

Configura `SMOKE_ADMIN_EMAIL` y `SMOKE_ADMIN_PASSWORD` con una cuenta administradora ficticia local antes de ejecutar los scripts. No incluyas secretos reales. `smoke.py` reconecta Microsoft y crea un alumno y reserva adicionales y verifica login, OAuth con state y PKCE, callback y Graph falsos, sincronización del worker y acceso a la reunión local. El worker puede tardar más de un minuto. El simulador guarda eventos y tokens en memoria: al reiniciarlo desaparecen; la credencial persistida queda inválida y hay que reconectar. No se limpian volúmenes automáticamente. El simulador no autentica identidades reales ni implementa una reunión real.

Pruebas unitarias del simulador y transporte, sin red:

```sh
docker run --rm --network none --mount "type=bind,source=$(pwd)/local-microsoft-sandbox,target=/sandbox,readonly" -w /sandbox beciencia-teams-test:local python -m unittest -v test_server test_workflow test_transport
```

Los scripts `smoke_lifecycle.py` y `smoke_recreate.py` crean datos de prueba; el segundo además cambia el escenario pendiente del simulador. No ejecutarlos durante otras pruebas activas. Los tokens no se imprimen.
