# Motor de Valor de Reventa — Documento de Diseño

> **ESTADO (2026-06-29): EXPLORADO Y APARCADO.** Técnicamente viable, pero **alcance estrecho**
> y **dependiente del Mac** (no 24/7). Decisión del usuario: NO incorporar. Ver "Conclusión" al final.
> El prototipo funcional (parser Worten + matcher + motor) se conservó en la rama
> **`resale-engine-prototype`** por si se retoma. `main` queda limpio (sin código del motor).

**Objetivo:** para tiendas que NO muestran precio "antes" (Worten y futuras), estimar el
valor de mercado de un producto a partir de su MODELO y compararlo con el precio de la
tienda para detectar márgenes de reventa. Servir a Apple y otras marcas, pero validar primero.

**Estado: PROPUESTA. No se ha escrito código.**

---

## 0. Lo que YA existe (mapa de reutilización) — clave para acotar

| Componente existente | Qué hace | Reutilización |
|---|---|---|
| `normalize_title()` (market_price.py) | lowercase, sin acentos, normaliza GB/TB, **quita condición (reacondicionado) y color**, ordena tokens | **Base del matching.** Un título refurb de Worten ya normaliza ≈ al del nuevo |
| `market_prices` (tabla SQLite) | cache `normalized_title → market_price` con `expires_at` (TTL) | **Almacén del valor de reventa.** TTL ya resuelve obsolescencia |
| `MarketPriceCache` | get exacto + fuzzy `token_set_ratio ≥ 90`, put con `ttl_days` | Reusable casi tal cual |
| `_lookup_cross_store()` | precio del MISMO producto en otras tiendas, match por `product_id` exacto o título fuzzy | **El corazón ya está hecho** |
| `_REFURBISHED_STORES = {backmarket, apple, cex}` | marca tiendas cuyo precio NO es comparable a nuevo | Ya existe conciencia del problema refurb-vs-nuevo |
| `get_*_by_product_id_*` (database.py) | stats de precio cruzadas por producto | Reusable |

**Conclusión:** esto NO es un sistema nuevo. Es **(1) un parser de Worten + (2) un matcher
estricto por specs + (3) reinterpretar el precio cruzado existente como "valor de reventa"**.
El grueso de la fontanería ya está.

---

## 1. FUENTE DEL VALOR DE REVENTA — decisión

**Recomendación: opción (a), reusando `_lookup_cross_store`.** Detalle y honestidad:

- **(a) Derivar de mi price_history (precio del MISMO modelo en mis tiendas que SÍ dan precio:
  Amazon, ECI, MediaMarkt).** VIABLE y barata: reutiliza `_lookup_cross_store` + `market_prices`
  + las 3,8M filas. Se autoactualiza cada ciclo.
  - **Matiz honesto crítico:** el cruce da el **precio NUEVO**, no el de reventa. Para Apple eso
    es un **proxy conservador útil**: un MacBook refurb que está al **≤60% del precio nuevo** es
    casi seguro flip (la reventa Apple ronda 0,80–0,90× nuevo). Es decir, **tu umbral del 60% que
    ya decidiste, aplicado contra el precio NUEVO cruzado, ES la señal de reventa.** No hace falta
    modelar el valor de segunda mano exacto para Fase 1: basta "¿está MUY por debajo del nuevo?".
  - **Precisión esperada:** el VALOR (precio nuevo cruzado) es fiable (±5-10%) cuando hay match.
    **El error NO está en el valor — está en el emparejamiento (punto 2).**
  - Requiere que el modelo exista en Amazon/ECI/MediaMarkt como NUEVO. Para MacBooks: sí, siempre.

- **(b) Externa (Keepa / sold listings de eBay).** Más precisa para reventa REAL (un "sold" de eBay
  ES el valor de reventa, no el nuevo). Pero: API de pago (Keepa ~15-20€/mes), integración +
  mantenimiento + rate limits + otro punto de fallo. **NO en Fase 1.** Solo si (a) demuestra
  precisión insuficiente. Sería una mejora de la FUENTE, enchufable al mismo motor.

- **(c) Tabla manual. DESCARTADA** con argumentos: no escala a "otras marcas/tiendas", envejece
  rápido (un modelo nuevo de Apple tira la reventa del anterior y habría que editar a mano),
  alto mantenimiento, y contradice el objetivo de generalizar. *Excepción menor:* una mini-tabla
  de **ratio reventa/nuevo por categoría** (Apple 0,85; consola 0,75; …) como AJUSTE opcional
  sobre (a), no como fuente. Opcional, no Fase 1.

---

## 2. EMPAREJAMIENTO POR MODELO — el riesgo técnico real

Worten: `"Macbook Air 15 Apple Medianoche (reacondicionado - Señales de uso - M2 - Ram 24GB 1TB SSD)"`.
`data-sku=MRKEAN-…` (EAN de marketplace, **no casa** con el EAN del nuevo).

**Estrategia:** NO usar el fuzzy genérico (peligroso aquí). Usar un **matcher estructurado por specs**:
1. Extraer del título, con regex, los **ejes que definen precio**: línea (MacBook Air/Pro), tamaño
   (13/15/16"), chip (M1/M2/M3/M4 + Pro/Max), RAM (8/16/24/36 GB), SSD (256GB/512GB/1TB).
2. Construir una **clave canónica** p.ej. `macbook-air-15-m2-24gb-1tb`.
3. Buscar en mis datos el MISMO canónico (nuevo) → ese es el valor.

**El peligro nº1 = FALSOS EMPAREJAMIENTOS → márgenes falsos → compras malas.** Ejemplos letales:
- Emparejar 256GB↔512GB, o 8GB↔24GB RAM, o M2↔M3: cientos de € de diferencia → "margen" inventado.
- El `token_set_ratio ≥ 90` actual SÍ caería en esto (alto solape de tokens). **Por eso no se usa el fuzzy genérico.**

**Regla de oro: FAIL CLOSED.** Si no se extraen con confianza chip + RAM + SSD, **no se emite señal**
(preferimos perder una oferta a inventar un margen). Tasa de error esperada honesta:
- Con match estricto exacto de specs: **falsos positivos bajos (~5%)**, pero **cobertura parcial**
  (40-60% de títulos Worten — muchos omiten RAM/SSD o usan formatos raros). Lo no-emparejado se
  descarta en silencio (seguro).
- Métrica que mediremos en Fase 1: **precisión del match** (% de señales con modelo correctamente
  emparejado) y **cobertura** (% de items con match confiable).

---

## 3. OBSOLESCENCIA — evitar valores envejecidos

- **TTL ya existe** (`market_prices.expires_at`). Reusar. Para Apple, acortar a **3-5 días**
  (la reventa Apple cae rápido, sobre todo en lanzamiento de gen nueva).
- Exigir que el valor venga de **observaciones recientes** (últimos N días) y un **mínimo de
  observaciones** (reusar `min_observations`) para no fiarse de un dato suelto.
- **Riesgo específico:** lanzamiento de MacBook nueva gen hunde la reventa de la anterior de un día
  para otro → baseline obsoleto → margen falso. Mitigación: TTL corto + el propio cruce con tiendas
  que SÍ dan precio se actualiza solo (Amazon/ECI bajan el nuevo → el valor baja con ellos).
- Recálculo: **continuo** (cada ciclo refresca al ver precios nuevos) + expiry fuerza refresco.

---

## 4. ALCANCE INICIAL ACOTADO — Fase 1 (validación)

**SOLO: Apple + Worten + MacBooks.** Nada más hasta validar.

Entregables Fase 1:
1. **Parser mínimo de Worten** (`WortenStore`): título, url, precio (`data-cnstrc-item-price`),
   sku. Selectores ya conocidos (`article.product-card`, `a.w-app-link`). Corre en el **Mac**.
2. **Matcher de specs de MacBook** (extracción línea/tamaño/chip/RAM/SSD → clave canónica) con
   FAIL CLOSED.
3. **Lookup de valor**: clave canónica → precio NUEVO cruzado (reusa `_lookup_cross_store` /
   `market_prices`).
4. **Señal**: si `precio_worten ≤ (1 - 0,60) × valor_nuevo` → candidato flip.
5. **Canal de PRUEBA aislado** (NO los canales de producción): un canal "Apple Flip — validación"
   donde caen los candidatos CON el desglose (precio Worten, valor estimado, modelo emparejado,
   margen). Así validas a mano sin contaminar tus alertas reales.

**Validación (1-2 semanas):** revisas cada candidato. ¿Match correcto? ¿Margen real? 
**Criterio de éxito:** p.ej. **≥70% de candidatos = match correcto con margen real**. Si pasa,
generalizamos (otras marcas/tiendas, ratio de reventa, quizá Keepa). Si no, abortamos (punto 5).

---

## 5. RIESGOS Y SEÑALES DE ABORTO

**Abortar si:**
- **Precisión de match < ~50%** (los falsos emparejamientos dominan) y el match estricto no mejora
  sin reglas por-modelo a mano (no escala).
- **Cobertura < ~20%** (casi ningún MacBook logra match confiable) → el motor casi nunca dispara →
  no compensa.
- El **precio nuevo cruzado resulta mal proxy** de la reventa de formas que no podemos modelar.
- El **parser de Worten se rompe seguido** (cambian su Nuxt/DOM) → mantenimiento alto.

**Qué lo haría inviable de raíz:** si en el caso MÁS fácil (MacBooks, modelos bien definidos) no
logramos matches fiables, entonces marcas con nomenclatura más sucia (portátiles PC, móviles
Android) son imposibles → se mata el proyecto entero. **MacBooks es el test de viabilidad.**

---

## 6. IMPACTO EN CARGA Y SISTEMA

- **NO toca el VPS estable.** Worten es Cloudflare → corre en el **Mac** (residencial). Toda la
  carga de scraping de Worten va al Mac, que está poco cargado.
- **El lookup de valor es una query SQLite indexada** (por `product_id`/`normalized_title`),
  en proceso, sin trabajo extra de navegador. Reusa el flujo `market_price` que ya corre por deal.
- **Trabajo añadido por ciclo:** parser Worten (unas URLs de búsqueda en el Mac) + 1 query de match
  por deal Worten. Despreciable.
- `market_prices` crece un poco (más claves). Irrelevante.
- **Riesgo de sistema:** el único acoplamiento nuevo es el matcher; si falla, FAIL CLOSED (no emite),
  no rompe nada existente.

---

## Resumen ejecutivo

- **Fuente recomendada: (a)** precio nuevo cruzado de mis propias tiendas (reusa todo). El umbral
  del 60% sobre el precio nuevo = la señal de reventa para Apple. Sin coste externo.
- **Riesgo nº1: falsos emparejamientos** → matcher estricto por specs + FAIL CLOSED.
- **Obsolescencia:** TTL corto (reusa `expires_at`) + observaciones recientes.
- **Fase 1: Apple + Worten + MacBooks + canal de validación aislado.** Validar precisión/cobertura
  antes de generalizar.
- **Carga: cero en el VPS** (Worten va al Mac); lookup = query barata.
- **Abortar si** precisión <50% o cobertura <20% en MacBooks (el caso fácil).

**Esfuerzo estimado Fase 1:** parser Worten (~como el de ECI) + matcher de specs + canal de prueba.
Acotado, sin pozo sin fondo, porque reutiliza el motor de precio existente y se valida en aislado.

---

## CONCLUSIÓN (2026-06-29) — Explorado y aparcado

Se construyó y validó en aislado el prototipo (sin cablear a producción). Hallazgos:

- **Matcher estricto: correcto.** Extrae specs (línea+tamaño+chip+RAM+SSD) de títulos
  spec-completos y hace FAIL CLOSED en accesorios/stubs. La baja cobertura bruta (6% sobre
  filas "macbook" de la DB) era RUIDO (accesorios Amazon, stubs sin specs de alternate), no
  fallo del matcher.
- **Solapamiento refurb↔nuevo (la pregunta de viabilidad): 4/6 modelos (67%).** Worten refurb
  hoy = solo Air **M1/M2** spec-completos; M1/M2 **se siguen vendiendo nuevos** (no hay gens
  huérfanas) → la fuente (a) precio nuevo cruzado SÍ sirve para el inventario actual.
- **Pero alcance ESTRECHO:** solo ~29% de deals Worten son spec-completos × 67% de solapamiento
  → ~15-20% del inventario valorable → pocos candidatos/ciclo. Y son **Air M1/M2 de gama
  media-baja**, NO el nicho de Mac caro del usuario.
- **Dependencia del Mac:** Worten es Cloudflare → corre en el Mac, que NO es 24/7.

**Decisión del usuario:** NO incorporar. El nicho de Mac caro ya lo cubren **ECI + MediaMarkt**
(muestran precio "antes", en producción, 24/7 en el VPS). Un motor estrecho de gama media en una
máquina no-24/7 no compensa.

**Si se retoma en el futuro:**
- Vía recomendada: **Keepa** (histórico de precios, incl. modelos descatalogados) — resuelve el
  decaimiento (cuando Apple descatalogue M2 nuevo, el cruce propio dejará de tener referencia).
- El prototipo (matcher por specs, parser Worten Nuxt, evaluación de margen comercial) está en la
  rama `resale-engine-prototype`, listo para reanudar.
- Para que valga la pena: ampliar a iPhones/iPads (más volumen) y/o resolver la dependencia del
  Mac (¿proxy residencial en el VPS para Worten?).
