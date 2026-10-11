---
id: dexter-making-of
enabled: true
title: Cómo se hizo Dexter
topic: how Dexter (this WhatsApp bot) was built - hosting, domain, Meta/WhatsApp setup and verification, Twilio, costs, architecture, status, roadmap, the repo
lang: [es, en]
triggers:
  - "como se hizo dexter"
  - "como hicieron a dexter"
  - "como hiciste a dexter"
  - "como se construyo dexter"
  - "como construyeron a dexter"
  - "como se creo dexter"
  - "como crearon a dexter"
  - "como te hicieron"
  - "como te construyeron"
  - "como te crearon"
  - "como te programaron"
  - "como estas hecho"
  - "de que estas hecho"
  - "detras de dexter"
  - "detras de camaras de dexter"
  - "making of dexter"
  - "el making of"
  - "como funciona dexter por dentro"
  - "arquitectura de dexter"
  - "quiero hacer un bot como dexter"
  - "estado de dexter"
  - "how was dexter built"
  - "how was dexter made"
  - "how did you build dexter"
  - "how are you built"
  - "how were you built"
  - "how were you made"
  - "dexter behind the scenes"
  - "dexter architecture"
  - "dexter status"
min_score: 0.75
notify_slack: true
pdf: true
---

<!--
"## Respuesta" is sent as-is on an explicit ask (Spanish); "## Response" is the English version.
Keep both under ~1,000 characters after rendering.
"## Detalle" joins the asker's agent context after disclosure, so follow-up questions
("¿y por qué no Twilio?", "¿cuánto cuesta?", "¿qué sigue?") get real answers, and goes into the PDF.
{{status_line}} and {{status}} are rendered live by app/status.py at disclosure time.
Never add secrets, tokens, IDs, IPs, SSH details or admin endpoints here.
-->

## Respuesta
🏆 Encontraste la puerta trasera del laboratorio. La curiosidad tiene premio: te cuento cómo me armaron. 🧪

Soy un bot de WhatsApp en Python/FastAPI que habla directo con la API oficial de Meta (sin intermediarios). Vivo en un servidor de DigitalOcean de 6 USD al mes, detrás del dominio de Alan. Mi cerebro es Claude (Anthropic), con la base de conocimiento de la plática. Cuando algo se sale de mi alcance, Alan entra a la conversación desde Slack.

Lo más interesante no fue el código, sino las decisiones: por qué no Twilio, cómo operar mientras Meta verifica el negocio, cómo mantener el costo casi en cero y cómo cumplir la política de Meta contra asistentes de IA genéricos.

{{status_line}}

El código está aquí como punto de partida: https://github.com/AlnEstrd/whatsapp-dexter

Pregúntame por cualquier parte: hosting, dominio, Meta y verificación, Twilio, costos, estado actual o el repo. Escribe *pdf* y te mando el expediente completo.

## Response
🏆 You found the lab's back door. Curiosity gets a reward: here's how I was built. 🧪

I'm a WhatsApp bot in Python/FastAPI talking straight to Meta's official API (no middleman). I live on a 6 USD/month DigitalOcean server behind Alan's domain. My brain is Claude (Anthropic) with the talk's knowledge base. When something is out of my league, Alan joins the chat from Slack.

The interesting part wasn't the code but the decisions: why not Twilio, how to run while Meta verifies the business, how to keep the cost near zero, and how to comply with Meta's policy against general-purpose AI assistants.

{{status_line}}

The code is here as a starting point: https://github.com/AlnEstrd/whatsapp-dexter

Ask me about any part: hosting, domain, Meta and verification, Twilio, costs, current status or the repo. Type *pdf* and I'll send you the full dossier.

## Detalle

### Estado actual (en vivo)
{{status}}

### Qué es Dexter
- Bot compañero de la plática "Gobernanza de IA" de Alan (15 min, líderes de PyMEs en México). La gente escanea un QR y llega a WhatsApp.
- Hace cinco cosas:
  - explorar la plática (preguntas sobre el contenido),
  - un diagnóstico rápido de madurez en IA (9 preguntas, 4 niveles, reporte PDF),
  - contar quién es Alan,
  - mandar láminas como imagen o PDF,
  - pasar la conversación a Alan.
- Responde en el idioma de quien escribe (ES/EN).
- Persona: Dexter, jefe del laboratorio de Estrada AI Lab.
- Tiene información que solo comparte si alguien la pide explícitamente (como esta). No aparece en el menú ni se sugiere: es el premio para los curiosos. Técnicamente: el modelo no puede sugerir lo que no ve, así que este contenido no está en su prompt hasta que alguien lo pide.

### Canal: Meta Cloud API directo (y por qué no Twilio)
- Requisito clave: cualquiera que escriba debe poder hablar con el bot, sin pasos previos.
- Por eso se descartaron dos opciones:
  - el Sandbox de Twilio (el usuario tiene que mandar un código para unirse),
  - el número de prueba de Meta (solo 5 destinatarios).
- Twilio sí es opción para producción, pero:
  1. Un número de producción en Twilio (o en cualquier BSP) también exige un portafolio de negocio en Meta y la verificación de Meta. Cambiar de proveedor no evita a Meta.
  2. Twilio cobra su propia tarifa por mensaje encima de la de Meta.
  3. Su ventaja real era Flex, que da una consola de agentes humanos. Slack resolvió el handoff gratis.
- Decisión: Meta Cloud API directo. El código de Twilio sigue en el repo como respaldo: cada proveedor es un "canal" intercambiable, así que cambiar es configuración, no reescritura.

### Meta: verificación y cómo operar mientras tanto
- Se puede operar como negocio **no verificado** mientras Meta revisa:
  - conversaciones iniciadas por el usuario: sin límite,
  - mensajes iniciados por el negocio: unos 250 usuarios únicos cada 24 h,
  - máximo 2 números.
  - Para un bot de evento, donde la gente escribe primero, alcanza.
- Pasos que funcionaron:
  1. Portafolio de negocio personal.
  2. App de Meta publicada en modo Live.
  3. Número real dedicado, registrado en la cuenta de WhatsApp Business.
  4. Nombre visible "Estrada AI Lab", enviado a revisión.
  5. Usuario de sistema con token permanente, no el token temporal de 24 h.
- Meta pide una política de privacidad pública. Se publicó en el sitio de Alan y la misma URL sirve como URL de eliminación de datos.
- El correo del dominio (Zoho Mail, plan gratuito) se usa como correo del negocio ante Meta.
- Política importante: Meta prohíbe asistentes de IA de propósito general en la API de WhatsApp Business. Dexter cumple de tres formas:
  - está acotado a la plática y al laboratorio,
  - tiene un filtro de temas fuera de alcance (avisa dos veces y luego pasa con Alan),
  - siempre tiene un humano detrás.
- Ventana de 24 h: solo se puede responder libremente dentro de las 24 h desde el último mensaje del usuario. Después, solo plantillas pagadas. Por eso el bot avisa en Slack cuando la ventana está por cerrarse.
- Si Meta rechaza la verificación, se corrige y se reenvía mientras se sigue operando como no verificado. No hay cambio de código.
- El estado de la verificación que ves arriba sale de los avisos que Meta le manda al bot (webhooks de cuenta), no de un texto escrito a mano.

### Dominio y hosting
- Dominio en Namecheap. El DNS se queda ahí porque el correo de Zoho ya estaba configurado.
- El sitio personal vive en Vercel. Para Meta, el bot es una ruta del sitio (`www.alanestrada.space/whatsapp-bot/api/...`): una regla de reescritura de Vercel la reenvía al servidor.
- Servidor: Droplet de DigitalOcean, el plan más chico que funciona (1 vCPU, 1 GB RAM, 6 USD/mes). Corre Docker Compose y Caddy (TLS automático) delante de la app.
- Por qué no las funciones de Vercel: el modelo serverless corta el trabajo después de responder y no guarda estado. Un bot de WhatsApp necesita las dos cosas: responder rápido a Meta y procesar en segundo plano.
- Por qué no DigitalOcean App Platform: su sistema de archivos es efímero.
- Truco: el dominio sin `www` redirige (308) y Meta no sigue redirecciones, así que el webhook siempre apunta a la URL con `www`.

### Cómo está construido
- Python 3 + FastAPI, un solo proceso.
- Cada proveedor (Meta, Twilio) es un canal: valida la firma, convierte el webhook a un mensaje común y envía respuestas.
- Agente: Claude con uso de herramientas.
  - El contenido central de la plática va en el prompt con caché, para pagar menos por repetirlo.
  - Hay búsqueda BM25 sobre las fuentes (DORA 2025, Acemoglu & Autor).
  - Herramientas para iniciar el diagnóstico, mandar láminas, generar PDFs de un tema y pasar con Alan.
- Diagnóstico de madurez: determinista, no lo decide el modelo. El reporte PDF se genera en Python (reportlab) y se manda como documento de WhatsApp, sin links públicos.
- Archivos que manda la gente (fotos, PDFs):
  - Claude los lee solo en ese turno; nunca se guardan en disco.
  - Hay límites de tamaño y se verifica el tipo real del archivo.
- Notas de voz: un servicio de voz a texto (compatible con la API de OpenAI) las transcribe y el texto sigue el flujo normal, como si la persona lo hubiera escrito. El audio no se guarda.
  - Si alguien cuenta cómo usa la IA en su empresa, Dexter puede precalificar el diagnóstico con lo que dijo y solo preguntar lo que falta.
- Slack: un hilo por conversación.
  - Alan solo recibe una mención cuando hay escalamiento.
  - Escribe `take` para tomar el control y `bot` para devolverlo.
  - Lo que escribe en el hilo le llega al usuario por WhatsApp.
- Ciclo de vida:
  - cierra la conversación cuando el usuario se despide,
  - avisa y luego cierra por inactividad,
  - si un reinicio borró el contexto, ofrece una disculpa.
- Pruebas: suite de pytest con HTTP simulado (el número actual sale en el estado de arriba).

### Endurecimiento y despliegue
Qué incluye cada pieza. Cuáles ya están activas lo dice "Estado actual".
- Persistencia en SQLite sobre un volumen, para que un redeploy no borre conversaciones ni diagnósticos a medias.
- Webhooks idempotentes: Meta reintenta, así que cada mensaje se procesa una sola vez.
- Topes de uso por persona y por día (mensajes, archivos y tokens), más un presupuesto global de tokens con alerta en Slack al 80%.
- Evals: un set de casos (saludos, preguntas de la plática, despedidas, temas fuera de alcance, intentos de inyección de prompt) para probar cambios de prompt o modelo antes de desplegar.
- CI/CD con GitHub Actions:
  1. pruebas en cada PR,
  2. al hacer merge a `main`, despliegue al droplet,
  3. health check público,
  4. rollback automático si falla,
  5. aviso en Slack.
  - Los secretos de la app nunca viven en GitHub.

### Qué sigue (roadmap)
- Láminas en inglés (hoy solo hay versión en español).
- Servir el PDF real de la presentación en lugar de las láminas regeneradas.
- Guardar los resultados del diagnóstico para darle seguimiento a quien lo pida después de la plática.
- Trazas de cada conversación (guard, pasos del agente, herramientas) para depurar y medir costo.

### Costos (orden de magnitud)
- Servidor: 6 USD/mes.
- Sitio en Vercel y correo en Zoho (plan gratuito): sin costo adicional relevante para el bot.
- WhatsApp (Meta): desde el 1 de octubre de 2026 Meta también cobra por mensaje las respuestas del negocio dentro de la ventana de 24 h (antes eran gratis). Lo que escribe el usuario no cuesta. Las plantillas fuera de la ventana se pagan aparte. Las tarifas vigentes están en el panel de Meta.
- Modelos (Anthropic): Claude Sonnet para el agente y Claude Haiku para el filtro de temas. El caché de prompt y los topes por usuario mantienen el gasto acotado; para un evento es de pocos dólares.
- Twilio habría sumado su tarifa por mensaje encima de la de Meta.
- Dominio: costo anual normal de Namecheap.

### Lecciones (los "gotchas")
- El App Secret de Meta es un valor hexadecimal de 32 caracteres en la configuración básica de la app. No es el token de acceso. Con el valor equivocado, todos los webhooks fallan con "firma inválida".
- La API key de Anthropic debe ser una key de la Consola, no una ligada a una cuenta de usuario. Con una key rechazada, cada mensaje se vuelve error del agente, se escala a Slack y el bot se queda callado.
- Responder rápido (200) a Meta y procesar en segundo plano; si no, Meta reintenta y duplica.
- El estado en memoria se pierde en cada redeploy. Hay que persistirlo desde el inicio.
- "Sería todo, gracias" es una despedida aunque no esté en la lista de palabras clave. La intención se reconoce por significado, no solo por palabras.

### Para usarlo como punto de partida
- Repo: https://github.com/AlnEstrd/whatsapp-dexter
- Ruta mínima:
  1. Clonar el repo y copiar `.env.example` a `.env`.
  2. Empezar con `AGENT_MODE=echo` para probar la plomería sin gastar tokens.
  3. Conectar un número de prueba de Meta.
  4. Correr las pruebas.
  5. Cambiar a tu propio número y a tu base de conocimiento en `app/kb/`.
- Lo que tendrías que cambiar: la base de conocimiento, el diagnóstico (`app/flows/quiz.py`), la persona del bot y el destino de escalamiento.
- Para dudas o para hacer algo parecido en tu empresa, escribe *humano* y Alan te contacta.
