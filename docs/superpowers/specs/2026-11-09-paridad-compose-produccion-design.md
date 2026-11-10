# Paridad del entorno local con producción

## Objetivo
El entorno habitual Docker Compose debe probar los artefactos y la ruta de servicio de la aplicación de manera lo más próxima posible a producción, para detectar antes de desplegar diferencias como el MIME del worker PDF.

## Alcance acordado
- Construir frontend y backend con sus Dockerfiles de producción; frontend compilado servido por Nginx y backend sin recarga ni montaje de código fuente. El worker usa la misma imagen de backend.
- Preservar PostgreSQL y archivos locales, datos y aislamiento. Mantener Microsoft y correo simulados; no usar servicios, datos ni credenciales de producción.
- Hacer que el tráfico de aplicación atraviese Nginx como en producción; conservar la simulación de Microsoft de forma acotada. Documentar cualquier diferencia inevitable del gateway, CSP, imágenes y configuración.
- Tras cada cambio de código, reconstruir las imágenes para probarlo. No usar el servidor de desarrollo de Vite para este entorno.
- Verificar servicios, autenticación y PDF real a través del acceso local habitual; la respuesta del worker debe ser JavaScript. No borrar volúmenes ni reiniciar el simulador sin consentimiento: los eventos y tokens ficticios en memoria se perderían.

## Alternativas
- Kubernetes local: más cercano al orquestador real, descartado por ahora por decisión del usuario.
- Docker Compose con artefactos de producción: elegido; no replica por completo Kubernetes, ingress, DNS, TLS ni servicios externos.

## Límites
No prometer identidad completa con producción ni tocar producción. No cambiar contratos de API ni debilitar controles de seguridad. La variante opcional de Microsoft real y su separación deben conservarse o informar explícitamente de cualquier limitación antes de modificarla.
