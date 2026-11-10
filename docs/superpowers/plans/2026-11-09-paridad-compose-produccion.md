# Paridad Docker Compose con producción — plan

> Ejecución: `superpowers:executing-plans`; no despachar agentes, confirmar ni publicar sin autorización expresa.

**Objetivo:** probar el entorno local habitual con imágenes de aplicación construidas a partir de los Dockerfiles de producción, con Nginx, sin Vite dev ni recarga del backend.

**Arquitectura:** conservar Compose, red aislada, PostgreSQL y volúmenes existentes y simulaciones locales. Encaminar tráfico de aplicación mediante Nginx; preservar una frontera explícita de simulación Microsoft y Mailpit. Los overlays de Microsoft real requieren comprobación independiente, sin conectarse a servicios reales.

**Tecnologías:** Docker Compose, Nginx, FastAPI, PostgreSQL, Node, pytest.

**Diseño aprobado:** `backend/docs/superpowers/specs/2026-11-09-paridad-compose-produccion-design.md`.

## Restricciones y riesgos
- No tocar producción, credenciales, secretos, ni ejecutar `down -v`. No eliminar volúmenes ni datos existentes.
- Un reinicio del simulador borra eventos y tokens ficticios en memoria: obtener consentimiento específico antes de reiniciarlo. Preferir validación aislada con volúmenes temporales sin afectar al entorno existente.
- No sustituir la versión de PostgreSQL ni cambiar datos sin contrastar manifiestos versionados y pedir permiso si hay impacto.
- Mantener funcionales simulación Microsoft, Mailpit, aislamiento de red y overlay opt-in de Microsoft real; no realizar autenticaciones reales.
- Mantener CSP, restricciones de origen y no debilitar seguridad. Identificar diferencias de Caddy/ingress, TLS, gateways, Kubernetes y simulaciones sin afirmar paridad completa.
- El gateway actual manda `/api/` directamente al backend: comprobarlo y modificar el flujo para que Nginx maneje rutas de aplicación, manteniendo las excepciones imprescindibles del simulador.
- Las variables `VITE_*` son de compilación: verificar configuración del modo simulado frente al artefacto publicado, sin asumir que una variable de ejecución cambia el bundle ni contaminar la imagen de producción con flags locales.

## Trabajo 1 — Contratos de Compose y simulador
**Archivos:** `docker-compose.local.yml`, `docker-compose.microsoft-real.local.yml`, `local-microsoft-sandbox/proxy.js`, pruebas existentes en `tests/` y `local-microsoft-sandbox/`.

- [ ] Inspeccionar de forma acotada Dockerfiles, routing, overlay y modo simulado; no exponer valores de secretos. Determinar cómo preservar la inyección del simulador y el modo real opt-in sin cambiar la imagen de aplicación.
- [ ] Escribir pruebas de regresión que demuestren frontend basado en Dockerfile de producción, backend sin reload ni montaje de código, worker con la misma imagen, volúmenes locales persistentes y red aislada. Añadir pruebas HTTP reales del routing `/api/`, `/fake/`, callback, CSP y MIME a través del gateway y Nginx; cubrir fallo de upstream y rechazos de hosts/paths no permitidos.
- [ ] Ejecutar las pruebas y confirmar que fallan por Vite/ruta actual, no por entorno inválido.
- [ ] Aplicar el cambio mínimo a Compose y routing para satisfacer el diseño. Preservar overlay real, sin abrir accesos externos nuevos ni relajar CSP; mantener almacenamiento separado y la compatibilidad del simulador.
- [ ] Ejecutar pruebas de Compose, overlay, gateway y simulación existentes y nuevas; comprobar el renderizado de configuración sin imprimir variables sensibles.

## Trabajo 2 — Verificación de extremo a extremo y documentación
**Archivos:** `local-microsoft-sandbox/RUN.md`, pruebas pertinentes y, solo si se necesita, CI existente.

- [ ] Construir imágenes locales con los Dockerfiles y levantar una instancia aislada con datos ficticios sin tocar volúmenes ni contenedores existentes. Verificar HTTP 200 del frontend, `/api/health`, carga del worker `.mjs` con MIME JavaScript y ausencia de Vite. Verificar autenticación y renderizado real de PDF en navegador sin exponer credenciales; si la prueba requiere participación humana, indicar el acceso y pedirla.
- [ ] Verificar worker y correo simulado, y mantener las pruebas de aislamiento de Microsoft. Documentar las diferencias que queden y cualquier comprobación no realizada.
- [ ] Actualizar instrucciones del entorno habitual para construir/arrancar, reconstruir tras cambios y conservar datos; retirar indicaciones de Vite como servidor habitual, sin eliminar documentación de otros flujos.
- [ ] Ejecutar suite relevante de backend, pruebas de proxy, frontend type-check/build y prueba de imagen/mime; revisar `git diff --check` y el estado de ambos repositorios. Registrar resultados verificables en el tablero, sin secretos.
- [ ] Solicitar revisión humana y autorización separada antes de cualquier commit, push, cambio de infraestructura externa o despliegue.
