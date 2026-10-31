# Chemistry Academy — reglas para el agente de backend

## Alcance y flujo de trabajo
- Este repositorio contiene el backend Python/FastAPI. Mantén los cambios dentro de este repositorio, salvo que el usuario solicite explícitamente coordinar cambios en otros.
- Trabaja en la rama actual. No cambies de rama, crees commits, hagas push o merge, publiques imágenes ni despliegues sin autorización expresa del usuario.
- Antes de editar, revisa `git status`. Conserva todos los cambios existentes del usuario; nunca hagas reset, limpieza ni sobrescrituras.
- Para funcionalidades que afecten a varios repositorios, acuerda el contrato de API y los criterios de aceptación antes de implementar. Mantén los cambios acotados y explica las dependencias entre repositorios.

## Tecnologías y convenciones
- Usa los patrones existentes de FastAPI, SQLAlchemy, Pydantic y configuración. No reemplaces la arquitectura ni añadas un framework sin aprobación.
- Antes de cambiar comportamientos, lee `README.md`, el código pertinente, las pruebas y el flujo de CI.
- Añade o actualiza pruebas para cambios de comportamiento, especialmente autenticación, autorización, reservas, concurrencia y migraciones de datos.
- Nunca trates los datos de demostración como datos de producción. No ejecutes comandos destructivos sobre bases de datos o volúmenes de producción.

## Validación
- Usa los comandos documentados en el repositorio y el flujo de CI. Como mínimo, ejecuta las pruebas de backend pertinentes y las comprobaciones de sintaxis de Python para el código modificado.
- Informa de los comandos exactos y sus resultados. Indica claramente qué pruebas no se ejecutaron y por qué.

## Seguridad y publicación
- No abras, imprimas, copies ni confirmes en Git archivos `.env`, credenciales, tokens, claves privadas ni secretos de producción descifrados. No incluyas secretos en instrucciones ni informes.
- No debilites la autenticación, autorización, validación ni gestión de secretos para hacer pasar las pruebas.
- El acceso a producción, los cambios de secretos, la publicación de imágenes, los merges a `main` y los despliegues requieren autorización expresa del usuario.
