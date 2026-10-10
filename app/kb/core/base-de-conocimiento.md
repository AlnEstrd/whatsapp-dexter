# Base de conocimiento — Gobernanza de IA y cambio organizacional

2026-10-06 · Alan Estrada

## Tesis central

La IA transforma actividades, no responsabilidades: un rol es la suma de responsabilidades (lo que se responde) y actividades (lo que se ejecuta), y solo las segundas son delegables a una máquina. El ownership y la autoridad se quedan en la organización, es decir, en una persona con nombre.

Tres afirmaciones sostienen la tesis, cada una con respaldo externo:

1. **La IA opera a nivel de tarea, no de puesto.** La economía laboral moderna analiza el impacto tecnológico por tareas, no por ocupaciones ([Acemoglu y Autor, 2011](https://www.nber.org/papers/w16082)). Por eso la unidad de diseño es la actividad.
2. **La accountability no es delegable.** NIST pide que el liderazgo ejecutivo "tome responsabilidad" por las decisiones de riesgo de IA y que se definan roles para las configuraciones humano-IA ([NIST AI RMF, GOVERN 2.3 y 3.2](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/)). El AI Act exige que personas físicas puedan supervisar, anular o detener un sistema de alto riesgo ([Art. 14](https://artificialintelligenceact.eu/article/14/)).
3. **El valor depende del sistema humano, no de la herramienta.** BCG encuentra que los líderes invierten 70% de sus recursos en personas y procesos ([BCG, 2024](https://bcg.com/ja-jp/press/24october2024-ai-adoption-in-2024-74-of-companies-struggle-to-achieve-and-scale-value)); DORA describe la IA como un amplificador de fortalezas y disfunciones existentes ([DORA, 2025](https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025)).

Frase ancla para la audiencia: _"Una computadora nunca puede ser responsable; por lo tanto, una computadora nunca debe tomar una decisión de gestión"_ (manual interno de IBM, 1979; cita ampliamente difundida, sin fuente primaria pública verificable).

## Precedente histórico: cuando los "computers" eran personas

Durante más de 300 años, "computer" fue un puesto de trabajo, no una máquina. Cuando llegó la computadora electrónica, la aritmética se automatizó, pero la responsabilidad por resultados correctos no desapareció: se movió a roles nuevos (programador, analista numérico, verificador) y obligó a rediseñar divisiones, habilidades y jerarquías. Es la misma historia que vivimos con la IA.

El primer uso registrado en inglés es de 1613: Richard Braithwait, en _The Yong Mans Gleanings_, escribe sobre "el computer más verdadero… el mejor aritmético que jamás haya respirado" ([Centre for Computing History](https://computinghistory.org.uk/det/5829/Richard%20Braithwaite%20first%20uses%20the%20word%20'computer')). Un computer era quien computaba.

| Año | Episodio | Qué cambió en la organización | Lección para la IA |
|---|---|---|---|
| 1793–1801 | Gaspard de Prony produce tablas logarítmicas para el catastro francés con unos 90 computers, muchos de ellos peluqueros desempleados tras la Revolución ([History of Information](https://www.historyofinformation.com/detail.php?id=439)) | Tres niveles inspirados en la fábrica de alfileres de Adam Smith: matemáticos diseñan fórmulas, planificadores escriben instrucciones, computers ejecutan. Inspiró la máquina diferencial de Babbage | La separación responsabilidad / actividad ya existía hace 230 años: quien ejecuta no es quien responde por el método |
| 1945–1946 | Seis computers de la Moore School que calculaban tablas de tiro (McNulty, Bilas, Jennings, Snyder, Lichterman, Wescoff) programan el ENIAC sin manuales ([ETHW](https://ethw.org/Women_Computers_in_World_War_II)) | La actividad de calcular la hace la máquina; nace el rol de programador. Pero fueron retiradas del proyecto cuando el ENIAC se mudó a una base militar | Las personas que dominan la actividad son las mejores para diseñar el nuevo rol; si la organización no las reconoce, pierde ese conocimiento |
| 1949–1958 | Dorothy Vaughan dirige West Area Computing en la NACA, la unidad segregada de mujeres afroamericanas computers ([NASA](https://www.nasa.gov/people/dorothy-vaughan/)) | Al llegar las computadoras IBM y nacer la NASA (1958), las unidades se disuelven y se crea la Analysis and Computation Division. Vaughan se vuelve experta en FORTRAN | Autoliderazgo y liderazgo: anticipó el cambio, se recapacitó y llevó a su equipo al nuevo rol |
| 1962 | Antes de la órbita de John Glenn (Friendship 7), él pide que Katherine Johnson verifique a mano los cálculos de trayectoria de la IBM 7090: "Si ella dice que los números están bien, estoy listo" ([Space.com](https://space.com/35218-hidden-figures-when-did-john-glenn-ask-for-the-girl-to-check-the-numbers.html)) | La máquina ejecuta; un humano con criterio valida antes de una decisión crítica | La confianza en la salida de una máquina se construye con supervisión humana; la accountability no se delega |

**Cómo usarlo en la charla:** abre con la pregunta "¿quién era una _computer_ en 1950?". Luego muestra que la tecnología no eliminó el trabajo, sino que lo reorganizó: la actividad pasó a la máquina, la responsabilidad subió de nivel y las organizaciones que ganaron fueron las que rediseñaron roles y recapacitaron a su gente. Hoy, con la IA, los "computers" somos todos.

Notas de precisión: la fecha exacta de la verificación de Johnson no está documentada (entre dic 1961 y feb 1962; fuente principal: Margot Lee Shetterly, _Hidden Figures_, 2016). Para profundizar: David Alan Grier, _When Computers Were Human_ (Princeton University Press, 2005).

## Marco Rol–Responsabilidad–Actividad

Un rol se rediseña actividad por actividad, pero su ownership nunca se reparte con la máquina. La IA asiste o ejecuta; el humano decide, responde y conserva la autoridad para intervenir.

Definiciones operativas para la charla:

- **Rol** — posición con mandato organizacional; es la suma de responsabilidades y actividades.
- **Responsabilidad** — resultado por el que alguien responde y tiene autoridad (accountability). No es delegable a IA.
- **Actividad** — unidad de trabajo ejecutable. Puede ser humana, asistida por IA o delegada a un agente.

**Matriz ilustrativa: rol de Gerente de compras** (ejemplo para adaptar; los niveles siguen tu escala L0–L3)

| Actividad | Ejecuta | Accountable | Nivel |
|---|---|---|---|
| Analizar cotizaciones y comparar proveedores | IA asistida | Gerente de compras | L1 |
| Generar órdenes de compra recurrentes | Agente | Gerente de compras | L3 |
| Detectar anomalías en facturas | Agente + revisión humana | Gerente de compras | L2 |
| Negociar contratos estratégicos | Humano | Gerente de compras | L0 |
| Definir política de proveedores | Humano | Director de operaciones | L0 |

La columna Accountable nunca contiene un agente: es la versión operativa de NIST GOVERN 3.2 (roles para configuraciones humano-IA). Cuando una actividad sube de nivel, cambia quién ejecuta, no quién responde; por eso el rediseño del rol debe incluir capacitación en supervisión y criterio, no solo en la herramienta.

## Por qué la adopción no puede saltarse el trabajo organizacional

Toda tecnología de propósito general rinde solo después de inversiones complementarias en procesos, habilidades y estructura; la IA no es la excepción. Los datos actuales muestran la brecha entre uso y valor:

| Hallazgo | Dato | Fuente |
|---|---|---|
| Empresas sin valor tangible de IA | 74% (solo 4% con capacidades de vanguardia) | [BCG, oct 2024](https://bcg.com/ja-jp/press/24october2024-ai-adoption-in-2024-74-of-companies-struggle-to-achieve-and-scale-value) |
| Pilotos de GenAI sin retorno medible | 95%; causa principal: brecha de aprendizaje e integración a flujos | [MIT NANDA, ](https://webberwentzel.com/News/Documents/2025/MIT1757411281972.pdf)[jul ](https://webberwentzel.com/News/Documents/2025/MIT1757411281972.pdf)[2025](https://webberwentzel.com/News/Documents/2025/MIT1757411281972.pdf) |
| High performers que rediseñaron flujos de trabajo | 73%; 2× más compromiso de la alta dirección | [McKinsey, State of AI](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai) |
| Proyectos de IA agéntica que se cancelarán a fines de 2027 | más de 40% (costos, valor poco claro, controles de riesgo) | [Gartner, jun 2025](https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-predicts-over-40-percent-of-agentic-ai-projects-will-be-canceled-by-end-of-2027) |
| Efecto de la IA en entrega de software | más throughput y también más inestabilidad | [DORA, 2025](https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025) |

**Paradigmas que explican la brecha**

- **Curva J de productividad** — las tecnologías de propósito general exigen inversión intangible (procesos, capacitación, rediseño) que primero deprime y luego eleva la productividad medida ([Brynjolfsson, Rock y Syverson](https://www.nber.org/papers/w25148)).
- **Sistemas sociotécnicos** — el desempeño surge de optimizar conjuntamente el sistema técnico y el social; optimizar solo la tecnología degrada el resultado (Trist y Bamforth, Tavistock Institute, 1951).
- **Modelos de cambio (Lewin, Kotter, ADKAR)** — todos ponen la visión, la coalición de liderazgo y el refuerzo antes de la herramienta. ADKAR (Prosci) aterriza el cambio en el individuo: Awareness, Desire, Knowledge, Ability, Reinforcement. Se mapea directo a tu lámina de OCM.
- **IA como amplificador** — DORA concluye que la IA "magnifica las fortalezas de las organizaciones de alto desempeño y las disfunciones de las que batallan". Sin trabajo organizacional, la IA acelera el desorden.

## Liderazgo y autoliderazgo como multiplicador

La IA sube el piso de ejecución; el techo lo pone el juicio de quien la dirige. Cuando la actividad se abarata, el diferencial se mueve a decidir qué hacer, validar el resultado y responder por él: eso es liderazgo y autoliderazgo.

**Evidencia**

- **La IA comprime la brecha de habilidad, no la elimina.** En 5,179 agentes de soporte, la IA subió la productividad 14% en promedio y 34% en novatos, con impacto mínimo en expertos; el modelo difunde las prácticas de los mejores ([Brynjolfsson, Li y Raymond, NBER](https://nber.org/papers/w31161)). Implicación: el criterio experto se vuelve el insumo escaso.
- **La frontera es irregular.** En 758 consultores de BCG, GPT-4 elevó la calidad más de 40% y la velocidad más de 25% dentro de la frontera de capacidad; fuera de ella el desempeño cayó ([Dell'Acqua et al., HBS](https://aiinstitute.hbs.edu/navigating-the-jagged-technological-frontier/)). Saber cuándo _no_ usar la IA es una competencia de autoliderazgo. Los perfiles "centauro" (divide y delega) y "cyborg" (integra) son modos de trabajo, no niveles técnicos.
- **El rol de todos pasa a ser "jefe de agentes".** Microsoft proyecta que los equipos entrenarán (41%) y gestionarán (36%) agentes en cinco años, y pide definir la proporción humano-agente por rol ([Work Trend Index 2025](https://blogs.microsoft.com/blog/2025/04/23/the-2025-annual-work-trend-index-the-frontier-firm-is-born/)). Gestionar agentes es delegar, especificar y revisar: habilidades de liderazgo, no de prompting.
- **La confianza es ****parcial**** y debe gestionarse.** 30% de los profesionales tiene poca (23%) o nula (7%) confianza en la calidad de lo que genera la IA y solo 24% reporta mucha o bastante; DORA lo lee como necesidad de habilidades críticas de validación ([DORA, 2025](https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025)). Calibrar confianza es juicio, no herramienta.
- **Autoliderazgo como teoría formal.** Manz define el autoliderazgo como procesos de autoinfluencia (estrategias conductuales, de recompensa natural y de pensamiento constructivo) con los que una persona se dirige y motiva a sí misma ([Manz, 1986](https://works.bepress.com/charles_manz/10); revisión de [Neck y Houghton, 2006](https://www.emerald.com/insight/content/doi/10.1108/02683940610663097/full/html)).

**La IA ya hace trabajo "mínimamente suficiente", no superior.** MIT FutureTech pidió a trabajadores con experiencia real evaluar resultados de LLM en tareas de O*NET. Con datos a finales de 2025, la IA llegó a "mínimamente suficiente" (7 de 9: útil tal cual, sin ediciones) en cerca de 65% de las tareas de texto, pero nunca pasó de 50% en calidad "superior" (9 de 9). El avance es una "marea que sube" (+~11 puntos al año en todas las tareas), no "olas que rompen" en tareas aisladas ([Mertens, Thompson et al., MIT FutureTech](https://futuretech.mit.edu/publication/crashing-waves-vs-rising-tides-preliminary-findings-on-ai-automation-from-thousands-of-worker-evaluations-of-labor-market-tasks); [Fortune, abr 2026](https://fortune.com/2026/04/03/mit-finds-ai-mostly-produces-minimally-sufficient-work/)). Implicación: la IA absorbe la actividad suficiente; la diferencia entre suficiente y excelente depende del juicio humano de quien responde por el resultado.

**Formulación para la charla:** Rendimiento con IA = capacidad de la herramienta × calidad de liderazgo × autoliderazgo. Si cualquiera de los factores es cero, el producto es cero.

## Orientación a negocio: del uso al yield

El uso de IA ya es casi universal; el retorno no. McKinsey reporta uso regular en casi 9 de cada 10 organizaciones, pero solo 37% atribuye impacto positivo en EBIT y apenas 6% son high performers con 5% o más de EBIT atribuible ([McKinsey](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai)). El yield se captura en el rediseño del flujo, no en la licencia.

**Qué hacen distinto quienes capturan valor**

| Práctica | Evidencia |
|---|---|
| Asignan 70% del esfuerzo a personas y procesos, 20% a tecnología y datos, 10% a algoritmos | [BCG](https://bcg.com/ja-jp/press/24october2024-ai-adoption-in-2024-74-of-companies-struggle-to-achieve-and-scale-value): líderes con 1.5× crecimiento de ingresos y 1.6× retorno al accionista en 3 años |
| Rediseñan flujos de punta a punta | [McKinsey](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai): 73% de los high performers |
| Tienen patrocinio ejecutivo y medición definida | McKinsey: 2× más probable en high performers |
| Apuntan a operaciones, no solo a marketing | [MIT NANDA](https://webberwentzel.com/News/Documents/2025/MIT1757411281972.pdf): más de la mitad del gasto va a ventas y marketing, pero el mayor ROI está en back office (US$2–10 M anuales al eliminar BPO) |
| Compran y adaptan antes de construir | [MIT NANDA](https://webberwentzel.com/News/Documents/2025/MIT1757411281972.pdf): alianzas con proveedores externos llegan a despliegue 66% de las veces; desarrollos internos, 33% |

**Implicación para el marco:** cada actividad candidata a IA se prioriza por valor de negocio (costo, tiempo de ciclo, calidad, riesgo), no por factibilidad técnica. Tu pirámide de medición (Engagement → Adopción → Desempeño individual → Desempeño de equipo) es la cadena causal que conecta uso con yield; la falla típica es quedarse midiendo la base.

## Gobernanza: marcos de referencia

Los tres marcos más citados coinciden en el punto central de la charla: la responsabilidad se asigna a personas y la supervisión humana se diseña, no se supone. La gobernanza es la visión que dirige el programa, no un freno.

| Marco | Qué dice sobre ownership y supervisión | Uso en la charla |
|---|---|---|
| [NIST AI RMF 1.0](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/) (EE. UU., voluntario) | GOVERN 2.1: roles y responsabilidades documentados y claros. GOVERN 2.3: el liderazgo ejecutivo asume las decisiones de riesgo. GOVERN 3.2: roles definidos para configuraciones humano-IA | Base para la matriz rol–responsabilidad–actividad |
| ISO/IEC 42001:2023 (sistema de gestión de IA, certificable) | Cláusula 5 (Liderazgo): la alta dirección demuestra compromiso, fija política de IA y asigna roles, responsabilidades y autoridades | Convierte la gobernanza en un sistema de gestión auditable, como ISO 27001 |
| [EU AI Act, Art. 14](https://artificialintelligenceact.eu/article/14/) | Los sistemas de alto riesgo deben poder ser supervisados por personas físicas que entiendan sus límites, estén alertas al sesgo de automatización y puedan anular o detener el sistema | El "botón de stop" como símbolo de autoridad humana |
| [EU AI Act, Art. 4](https://digital-strategy.ec.europa.eu/en/faqs/ai-literacy-questions-answers) | Proveedores y usuarios deben asegurar un nivel suficiente de alfabetización en IA de su personal; aplica desde feb 2025 | Respaldo regulatorio de la capa Individual (fluidez en IA) |

Para empresas en México ninguno es obligatorio por sí mismo, pero el AI Act aplica a quien ofrezca sistemas o resultados de IA en la UE, y NIST/ISO son el lenguaje común con clientes globales.

## Cómo encajan los materiales actuales

Tus tres láminas ya cubren diagnóstico, cambio y medición; lo que falta es la capa que las une: quién es responsable de qué cuando la IA ejecuta. El marco rol–responsabilidad–actividad es ese hilo.

| Material actual | Qué aporta | Respaldo externo | Conexión con rol–responsabilidad–actividad |
|---|---|---|---|
| Diagnóstico Organización / Equipo / Individual | Dónde están las brechas de preparación en 3 niveles | 70% personas y procesos ([BCG](https://bcg.com/ja-jp/press/24october2024-ai-adoption-in-2024-74-of-companies-struggle-to-achieve-and-scale-value)); 7 capacidades organizacionales de [DORA](https://dora.dev/ai/capabilities-model); alfabetización en IA (AI Act Art. 4) | Org = dónde vive la autoridad; Equipo = cómo se reparten actividades humano-agente (L0–L3); Individual = quién puede asumir la responsabilidad con IA (brecha de apalancamiento) |
| OCM: seis obstáculos | Visión, alineación del liderazgo, involucramiento, KPIs, cultura, comunicación | Kotter, ADKAR; patrocinio ejecutivo 2× en high performers ([McKinsey](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai)) | Redefinir roles es el cambio que hay que gestionar: miedo a perder el puesto se responde mostrando qué responsabilidades permanecen |
| Pirámide de medición: Engagement → Adopción → Desempeño individual → Desempeño de equipo | Cadena causal de uso a resultado | Throughput vs. inestabilidad ([DORA](https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025)); EBIT ([McKinsey](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai)) | Las capas superiores miden responsabilidades cumplidas (resultado), las inferiores miden actividades (uso) |

**Ajustes sugeridos a las láminas**

- OCM: corregir "acaptacion" → "aceptación" y completar la frase "lo que las convierte en la palanca más…" (termina cortada).
- Pirámide: añadir una quinta capa arriba, **Resultado de negocio** (EBIT, costo por unidad, tiempo de ciclo), para cerrar en yield.
- Diagnóstico: usar los siete dominios definidos abajo para la capa Organización.

**Las 7 capacidades de IA de DORA y dónde caen en tu diagnóstico**

DORA concluye: "el valor de la IA no se obtendrá gracias a la tecnología en sí, sino redefiniendo el sistema de trabajo en el que se encuentra". Estas siete capacidades, elegidas de 15 candidatas, son las que demostraron amplificar el efecto de la IA. El reporte se basa en casi 5,000 profesionales encuestados entre jun y jul de 2025 (_Estado del desarrollo de software asistido por IA 2025_, versión abreviada en español, v. 2025.2.a). La asignación por nivel y la conexión con el marco son propuesta nuestra, no de DORA.

| Capacidad (DORA) | Nivel del diagnóstico | Conexión con el marco |
|---|---|---|
| Postura clara y comunicada sobre la IA | Organización | Paso 1, Dirección: la gobernanza como política que cada persona sabe que le aplica |
| Ecosistemas de datos en buen estado | Organización | Base del 20% de tecnología y datos (BCG) |
| Datos internos accesibles por la IA | Organización | Condición para delegar actividades con contexto |
| Plataformas internas de calidad | Organización | 90% de las organizaciones ya adoptó ingeniería de plataformas; DORA la llama la base esencial |
| Prácticas sólidas de control de versiones | Equipo | La red de seguridad para revertir: la autoridad para anular hecha práctica |
| Trabajar en lotes pequeños | Equipo | Mantiene la revisión humana manejable frente al volumen que produce la IA |
| Enfoque centrado en el usuario | Equipo | Orienta la velocidad hacia valor: el puente al yield |

Otros hallazgos útiles del mismo reporte:

- **Adopción:** 90% de los profesionales usa IA y más de 80% percibe más productividad.
- **Velocidad vs. estabilidad:** la IA ya mejora la velocidad de entrega, pero sigue aumentando la inestabilidad. En 2024, DORA estimó +7.2% de inestabilidad por cada 25% más de adopción.
- **Lo que no mejora:** no hay relación medible entre la IA y el agotamiento o las complicaciones. DORA lo atribuye al sistema organizacional, no a la herramienta.
- **Value Stream Management (VSM):** actúa como multiplicador de fuerzas para que las ganancias locales se vuelvan mejoras de equipo y producto.
- **Siete perfiles de equipo:** van de los "armoniosos de alto rendimiento" a los atrapados en un "cuello de botella heredado". Pueden enriquecer tu escala L0–L3.
- **Percepción vs. realidad (citado por DORA):** en un estudio de METR, desarrolladores 19% más lentos con IA creían haber ganado 20% de eficiencia. Es un argumento fuerte para medir resultados y no percepción.

**Los siete dominios de la capa Organización (propuesta)**

Cada dominio responde una pregunta de preparación y tiene respaldo en al menos una fuente de esta base. La selección y los nombres son propuesta nuestra, construida a partir de esas fuentes.

| # | Dominio | Pregunta de diagnóstico | Respaldo |
|---|---|---|---|
| 1 | Estrategia y valor | ¿Qué resultados de negocio persigue la IA y cómo se miden? | McKinsey (medición definida 2× en high performers); Tokenomics Foundation |
| 2 | Gobernanza y riesgo | ¿Hay política de IA, apetito de riesgo y supervisión humana definidos? | NIST AI RMF GOVERN; ISO/IEC 42001; DORA "postura clara y comunicada" |
| 3 | Liderazgo y patrocinio | ¿Un ejecutivo es dueño del programa y el liderazgo está alineado? | McKinsey (compromiso de la alta dirección 2×); NIST GOVERN 2.3; lámina OCM |
| 4 | Roles y ownership | ¿Cada actividad que pasa a IA tiene un responsable humano y un presupuesto? | NIST GOVERN 3.2; EU AI Act Art. 14; Tokenomics Foundation (3.7×) |
| 5 | Personas y cultura | ¿Hay alfabetización en IA, champions y una cultura que acepte el cambio? | EU AI Act Art. 4; BCG 70%; ADKAR |
| 6 | Procesos y flujos de trabajo | ¿Se rediseñan los flujos de punta a punta o solo se agrega IA a lo existente? | McKinsey (73% de high performers); DORA (VSM) |
| 7 | Datos y plataforma | ¿Los datos internos son confiables y accesibles, y hay una plataforma común? | DORA (ecosistemas de datos, datos accesibles, plataformas internas); BCG 20% |

Los dominios 3 y 4 son los que conectan el diagnóstico con la tesis: sin patrocinio y sin ownership, los otros cinco no generan yield.

## Marco integrado y key takeaways

El marco propuesto encadena cinco pasos: de la estrategia de negocio a la medición del yield, con la responsabilidad humana como constante. Cada paso usa un material que ya tienes o una fuente de esta base.

1. **Dirección (gobernanza como visión)** — definir qué resultados de negocio persigue la IA y qué riesgos se aceptan; el liderazgo ejecutivo firma (NIST GOVERN 2.3, ISO 42001 cl. 5).
2. **Descomposición** — por cada rol, separar responsabilidades de actividades e identificar qué actividades son candidatas a IA por valor y por frontera de capacidad (Acemoglu y Autor; Dell'Acqua).
3. **Asignación de ownership** — cada actividad delegada a IA o a un agente tiene un humano accountable con autoridad para anular o detener (AI Act Art. 14; NIST GOVERN 3.2).
4. **Habilitación de personas** — diagnóstico Org/Equipo/Individual + OCM + desarrollo de liderazgo y autoliderazgo (BCG 70%; ADKAR; Manz).
5. **Medición del yield** — pirámide de Engagement a Resultado de negocio; revisar trimestralmente y reasignar actividades (McKinsey; DORA).

**Key takeaways para la audiencia ejecutiva**

1. La IA se adopta por actividad; la responsabilidad se queda en el rol y en una persona con nombre.
2. Tres de cada cuatro empresas no ven valor porque invierten en la herramienta y no en el 70% que es personas y procesos.
3. La IA amplifica lo que ya existe: con buen liderazgo acelera resultados, sin él acelera el desorden.
4. El nuevo trabajo de todos es dirigir: especificar, delegar y validar el trabajo de agentes requiere autoliderazgo.
5. La gobernanza no frena ni solo habilita: es la visión estratégica que define hacia dónde va el programa.
6. Medir uso no es medir valor: la conversación con el consejo es sobre EBIT, tiempo de ciclo y riesgo.

**Primeros 90 días para empezar a modernizar**

- Nombrar un sponsor ejecutivo y publicar una postura clara sobre IA (política de uso)
- Elegir 2 o 3 roles críticos y hacer su matriz responsabilidad–actividad
- Correr el diagnóstico Org/Equipo/Individual en esas áreas
- Asignar un owner humano a cada actividad que pase a IA o agentes
- Fijar 1 o 2 KPIs de negocio por caso de uso antes del piloto

## Cross-check: Tokenomics Foundation

El marco se sostiene y gana un dato fuerte. La Tokenomics Foundation llega a la misma conclusión desde el lado financiero: sin un owner definido no hay valor demostrable. Su lema, "no cuentes tokens, realiza valor del costo total de la IA", es la versión FinOps de tu enfoque en yield ([tokeneconomics.com](https://www.tokeneconomics.com/)).

Quién es: un consorcio de la industria bajo LF Projects (Linux Foundation). Reúne a consumidores y proveedores de tokens: Accenture, IBM, JPMorgan Chase, SAP, ServiceNow, Oracle Cloud y Equifax, entre otros, con MIT, NYU y la Universidad de Washington como miembros académicos ([miembros](https://www.tokeneconomics.com/about/members/)). Se organiza en tres pilares: Producción, Consumo y Valor.

**Hallazgos del ****[State of Tokenomics](https://www.tokeneconomics.com/state-of-tokenomics/)**** (23 sep 2026, RC 1.0; 472 respuestas, 11 industrias)**

| Hallazgo | Dato | Qué confirma del marco |
|---|---|---|
| Empresas que no pueden demostrar resultados de IA al CFO | 3 de cada 4 | La brecha uso–valor (BCG 74%, MIT NANDA 95%) |
| Empresas con ownership definido de la economía de IA | 88% (35% en CTO/CIO, 26% compartido) | La responsabilidad debe tener nombre |
| Probabilidad de demostrar valor al CFO con ownership definido | 3.7× mayor | Paso 3: asignación de ownership |
| Empresas sin owner definido que conectan gasto con resultados | 0 de ese 12% | Sin owner humano no hay yield |
| Principal reto citado | 43%: demostrar valor y ROI; solo 4% pide precios más bajos | El problema es de gestión, no de costo de la herramienta |

**Coincidencias y diferencias**

- **Gobernanza como plano de control.** En su [stack de cinco capas](https://www.tokeneconomics.com/projects/the-five-layer-tokenomics-stack/), la capa L5 (ruteo y gobernanza) es "la única que puede decidir que una solicitud no necesita modelo" y la única que puede detener un consumo sin límite (circuit breakers). Equivale a tu paso 2 (no toda actividad debe ir a IA) y al "botón de stop" del AI Act.
- **Lente distinta, complementaria.** Su stack cubre silicio, capacidad, inferencia, modelos y ruteo: el lado técnico y de costo (el 20% de BCG). Excluye a propósito la rendición de cuentas humana y la creación de valor, que remite a otro pilar. Tu marco cubre ese 70% de personas y procesos.
- **Dónde vive el ownership.** En su encuesta, el owner suele ser tecnología (CTO/CIO). Tu tesis sugiere dividir: el área de negocio responde por el valor de cada caso de uso y tecnología por el costo y la eficiencia.

**Ajustes que sugiere al marco**

- Paso 3: el owner humano de cada actividad delegada también es dueño de su presupuesto de IA.
- Paso 5 y pirámide: medir costo total de IA por unidad de resultado (por ejemplo, costo por ticket resuelto incluyendo tokens), no tokens ni licencias consumidas.
- Key takeaway adicional: "Si nadie es dueño del gasto en IA, nadie puede demostrar su valor" (3.7×, Tokenomics Foundation).

Cautela al citar: el reporte es un release candidate y se basa en una encuesta autodeclarada. Además, el consorcio incluye proveedores con interés comercial en FinOps de IA.

## Fuentes

| Fuente | Tipo | Dato clave |
|---|---|---|
| [BCG — AI Adoption in 2024: 74% struggle to achieve and scale value](https://bcg.com/ja-jp/press/24october2024-ai-adoption-in-2024-74-of-companies-struggle-to-achieve-and-scale-value) ([PDF](https://media-publications.bcg.com/BCG-Wheres-the-Value-in-AI.pdf)) | Encuesta, 1,000 ejecutivos, 59 países | 74% sin valor; regla 10-20-70 |
| [McKinsey — The state of AI](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai) | Encuesta global anual | 37% con impacto EBIT; 6% high performers; 73% rediseñaron flujos |
| [MIT NANDA — The GenAI Divide](https://webberwentzel.com/News/Documents/2025/MIT1757411281972.pdf)[: State of AI in Business 2025](https://webberwentzel.com/News/Documents/2025/MIT1757411281972.pdf) (Challapally, Pease, Raskar, Chari; jul 2025) | 52 entrevistas, 153 encuestas a líderes, 300+ iniciativas públicas (ene–jun 2025) | 95% de pilotos sin retorno |
| [DORA — State of AI-assisted Software Development 2025](https://services.google.com/fh/files/misc/2025_state_of_ai_assisted_software_development.pdf) y [AI Capabilities Model](https://dora.dev/ai/capabilities-model) | Investigación, Google Cloud | IA como amplificador; 7 capacidades |
| [Gartner — 40% de proyectos agénticos cancelados a 2027](https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-predicts-over-40-percent-of-agentic-ai-projects-will-be-canceled-by-end-of-2027) | Predicción de analista | Riesgo de "agent washing" |
| [Microsoft — Work Trend Index 2025: The Frontier Firm](https://blogs.microsoft.com/blog/2025/04/23/the-2025-annual-work-trend-index-the-frontier-firm-is-born/) | Encuesta + telemetría | "Agent boss"; proporción humano-agente |
| [Brynjolfsson, Li y Raymond — Generative AI at Work](https://nber.org/papers/w31161) | Académico (NBER / QJE) | +14% promedio, +34% novatos |
| [Dell'Acqua et al. — Navigating the Jagged Technological Frontier](https://aiinstitute.hbs.edu/navigating-the-jagged-technological-frontier/) | Experimento, 758 consultores BCG | +40% calidad dentro de la frontera; centauros y cyborgs |
| [Brynjolfsson, Rock y Syverson — The Productivity J-Curve](https://www.nber.org/papers/w25148) | Académico (AEJ: Macro) | Inversión intangible complementaria |
| [Acemoglu y Autor — Skills, Tasks and Technologies](https://www.nber.org/papers/w16082) | Académico (Handbook of Labor Economics) | Enfoque por tareas |
| [Manz — Self-Leadership (1986)](https://works.bepress.com/charles_manz/10); [Neck y Houghton (2006)](https://www.emerald.com/insight/content/doi/10.1108/02683940610663097/full/html) | Académico | Teoría de autoliderazgo |
| [NIST AI RMF 1.0 — Core (GOVERN)](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/) | Marco voluntario | Roles, accountability, liderazgo ejecutivo |
| [EU AI Act — Art. 14](https://artificialintelligenceact.eu/article/14/) y [Art. 4 Q&A](https://digital-strategy.ec.europa.eu/en/faqs/ai-literacy-questions-answers) | Regulación | Supervisión humana; alfabetización en IA |
| ISO/IEC 42001:2023 | Estándar certificable | Cl. 5 Liderazgo: roles y autoridades |
| Kotter (1995), _Leading Change_, HBR; Prosci ADKAR; Trist y Bamforth (1951) | Clásicos de cambio organizacional | Visión, coalición, refuerzo; sistemas sociotécnicos |

Añadido: [MIT FutureTech — Crashing Waves vs. Rising Tides](https://futuretech.mit.edu/publication/crashing-waves-vs-rising-tides-preliminary-findings-on-ai-automation-from-thousands-of-worker-evaluations-of-labor-market-tasks) ([PDF, versión del 21 jul 2026](https://cdn.prod.website-files.com/6501f77fe3413fa9dbfbd69d/6a60d8d26a565e0d6983744a_Crashing_Waves_vs_Rising_Tides_%2021July26.pdf)). Académico, más de 6,000 tareas de O*NET y más de 60,000 evaluaciones de trabajadores. Proyecta entre 88% y 97% de tareas de texto con calidad aceptable para 2030. La nota de Fortune cita la versión preliminar de abril (41 modelos, más de 11,000 tareas, entre 80% y 95% para 2029). Para la charla, usa las cifras de julio.

Añadido: [Tokenomics Foundation — State of Tokenomics (sep 2026)](https://www.tokeneconomics.com/state-of-tokenomics/) y [Five-Layer Tokenomics Stack](https://www.tokeneconomics.com/projects/the-five-layer-tokenomics-stack/). Consorcio de industria (LF Projects), encuesta con 472 respuestas. Ownership definido = 3.7× más probabilidad de demostrar valor al CFO.

Añadido: DORA (Google Cloud), _Estado del desarrollo de software asistido por IA 2025_, versión abreviada en español (v. 2025.2.a; PDF compartido por Alan). Casi 5,000 profesionales encuestados entre el 13 de jun y el 21 de jul de 2025, más 78 entrevistas a profundidad. Fuente de las 7 capacidades y de los datos de confianza (30% poca o nula, 24% mucha o bastante), que reemplazan la cifra de 3% tomada de una nota de prensa.

Nota de citación: todas las fuentes se citan. Las cifras de McKinsey corresponden a la edición publicada en su sitio al 6 de octubre de 2026; cítala como "McKinsey, The State of AI (consultado oct 2026)".
