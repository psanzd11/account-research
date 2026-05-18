# Improvements Plan A — 7 Critical Fixes

> **Para Claude Code:** este archivo es el plan de trabajo. Léelo entero antes de empezar. Las convenciones del proyecto están en `CLAUDE.md` (raíz del repo) y la arquitectura en `SPEC.md`. **No las dupliques aquí.** Si algo de este plan contradice `CLAUDE.md`, gana `CLAUDE.md` y pregunta al usuario.

## Contexto

Pipeline multi-agente que genera PDF de research brief con citas verificables. La auditoría encontró que el pipeline está completo y estructuralmente sólido — los 7 cambios de abajo optimizan **eficiencia de tokens**, **precisión fina**, y **detección temprana de bugs**, sin alterar la arquitectura.

**Las reglas no-negociables de `CLAUDE.md` siguen vigentes** — ningún cambio puede violarlas (especialmente reglas 1, 3 y 5 sobre citas, rangos vs puntos, y `raw_quote` obligatorio).

## Workflow

1. **Una rama por Fase** (`improvements/phase-1`, `improvements/phase-2`, `improvements/phase-3`). Mergea a `main` solo cuando toda la fase está verde.
2. **Un commit atómico por tarea** (A1, A2, ...). Mensaje: `[A<n>] <descripción corta>`.
3. **Tests verdes antes de marcar tarea completa.** Si una tarea no tiene test, escríbelo antes de la implementación (TDD ligero).
4. **Cobertura de regresión:** correr las 5 entidades canónicas (`CLAUDE.md` sección "When testing") al final de cada fase. Comparar costo, latencia, y `issue count` del Reviewer contra baseline.
5. **Ambigüedad → preguntar.** Si un file:line no corresponde a lo descrito o el cambio rompe algo no previsto, **detente y pregunta**, no improvises.
6. **Las referencias `file:~NN` son aproximadas** (de una auditoría externa). Verifica con grep antes de editar.

## Métricas a capturar (baseline ↔ post)

Antes de empezar Fase 1, correr 1 brief de regresión y registrar en `docs/metrics-baseline.json`:
- Costo USD por brief (suma de Anthropic + Voyage).
- Latencia total (s).
- Tokens prompt / completion / cache_read (sum de las llamadas).
- Número de iteraciones del Reviewer.
- Conteo de items rejected / unverifiable / verified en el ledger.

Repetir al final de cada fase y diff.

---

## Fase 1 — Quick wins (~50 min de trabajo, ~30% ahorro costo/latencia)

### A1. Prompt caching en Author y Reviewer

**Por qué:** ledger (~20 KB) y system prompts se reenvían idénticos en cada iteración. Sin `cache_control`, no hay cache hit.

**Archivos:**
- `account_research/llm_client.py` (~líneas 93-240): extender wrapper para aceptar bloques cacheables. Aplicar `cache_control={"type": "ephemeral"}` al último bloque marcado.
- `account_research/agents/author.py` (~línea 40): marcar como cacheable el system prompt + serialización del ledger. El feedback de revisión va **fuera** del bloque cacheado.
- `account_research/agents/reviewer.py` (~línea 69): mismo patrón. El PDF text + ledger cacheados; issues previos fuera.

**Test:**
- Unit: mock del SDK, verificar que el request lleva `cache_control` en el bloque correcto.
- Integración: correr el mismo brief 2× consecutivamente, leer `cache_read_input_tokens` de la respuesta de la 2ª ronda — debe ser `> 0`.

**Aceptación:**
- `cache_read_input_tokens > 0` en iter 2+ del Reviewer y del Author.
- Costo por brief baja ≥15% en la regresión.
- Brief output idéntico al baseline (mismo ledger, mismo prose) — el caching no debe alterar resultados.

---

### A2. Vision mode automático en iter ≥2 del Reviewer

**Por qué:** el código de vision existe en `reviewer.py` pero `use_vision=False` por default. SPEC fase 6 indica que iter ≥2 debería usarlo para detectar problemas de layout/caveats invisibles.

**Archivo:**
- `account_research/orchestrator.py` línea ~62: cambiar a `use_vision=(iteration >= 2)` al instanciar el Reviewer.

**Test:**
- Unit en `tests/unit/test_reviewer.py` (crear si no existe el caso): mockear iteración 1 y 2, verificar que `use_vision` cambia.

**Aceptación:**
- Iter 1 sin vision (latencia baja), iter ≥2 con vision activado.
- Sin nuevas dependencias.

---

### A3. Designer pre-valida que `evidence_ids` del brief existan en el ledger

**Por qué:** si Author bugea y emite un `evidence_id` huérfano, el PDF se renderiza igual y solo el Reviewer lo detecta — perdiendo 1 iteración (~$5). Fail fast.

**Archivos:**
- `account_research/agents/designer.py` (~línea 56, antes de `build_brief`):
  ```python
  ledger_ids = {e.evidence_id for e in ledger.items}
  brief_ids = set(brief.evidence_ids_referenced())  # método helper si no existe
  orphans = brief_ids - ledger_ids
  if orphans:
      raise OrphanEvidenceError(orphans=orphans, ...)
  ```
- Definir `OrphanEvidenceError` en `account_research/agents/base.py` (o crear `account_research/errors.py` si crece).
- `account_research/orchestrator.py`: capturar `OrphanEvidenceError`, marcar la iter como fallida y forzar revisión del Author en la siguiente iter (con el `orphans` set como issue).

**Test:**
- `tests/unit/test_designer.py`: inyectar brief con UUID huérfano, verificar `OrphanEvidenceError`.
- `tests/integration/`: simular Author buggy, verificar que el orchestrator no llega a renderizar PDF.

**Aceptación:**
- Brief con IDs huérfanos nunca llega al PDF.
- El orchestrator usa la detección para gatillar revisión, no para crash.

---

## Fase 2 — Precisión (~3-4 horas)

### A4. Embeddings de Voyage como default en citation validator

**Por qué:** para LATAM (ES↔EN), Jaccard + 4-grams genera muchos false-negatives. El costo de Voyage es ~$0.001/brief vs ~$4 de un Author call — el default debería ser ON.

**Archivos:**
- `account_research/agents/citation_validator.py` (~líneas 60-81): invertir flag. Actualmente `USE_EMBEDDINGS_FOR_CITATIONS=1` activa; cambiar a `DISABLE_EMBEDDINGS_FOR_CITATIONS=1` para desactivar (default ON).
- Fallback gracioso: si Voyage timeout o error, log warning y caer a Jaccard. **No fallar la pipeline.**
- Cachear embeddings de `EvidenceItem` por hash de `raw_quote` (para no re-embeded en iter 2+).

**Test:**
- Unit con caso LATAM: quote ES, prose EN. Verificar que Voyage lo aprueba y Jaccard lo marca débil (o construye un caso similar realista).
- Test de fallback: simular Voyage down, verificar que el flujo sigue con Jaccard + log warning.

**Aceptación:**
- Voyage activo por default.
- Pipeline no falla si Voyage está down.
- Costo extra <$0.005/brief.

---

### A5. LCS ≥0.92 floor en Fact-Checker para `raw_quote`

**Por qué:** esta es la fix más importante de precisión. La regla 5 de `CLAUDE.md` exige `raw_quote` verbatim. Fact-Checker normaliza Unicode + fuzzy 0.78, pero no aplica un piso LCS antes de aceptar "unverifiable" como fallback. **Quotes modificadas sutilmente entran al ledger.**

**Archivos:**
- `account_research/schemas/evidence.py`: añadir `claim_similarity_score: float | None = None` al `EvidenceItem`.
- `account_research/agents/fact_checker.py` (~línea 200): antes del path de "unverifiable":
  ```python
  from difflib import SequenceMatcher
  lcs_ratio = SequenceMatcher(None, normalize(raw_quote), normalize(body)).ratio()
  item.claim_similarity_score = lcs_ratio
  if lcs_ratio < 0.92 and fuzzy_score < 0.78:
      item.status = "rejected"  # NO entra al ledger
      continue
  ```
- `account_research/ledger.py`: persistir `claim_similarity_score`.

**Test (crítico):**
- Caso A: raw_quote idéntica al body → `lcs_ratio ≥ 0.95`, status `verified`.
- Caso B: raw_quote con normalización Unicode trivial (acentos, dashes) → pasa.
- Caso C: raw_quote con 2 palabras cambiadas (sinónimos, singular→plural) → `rejected`.
- Caso D: raw_quote completamente parafraseado → `rejected`.

**Aceptación:**
- Test C falla antes del cambio, pasa después.
- `claim_similarity_score` se persiste en el ledger y se puede consultar.
- El Researcher coverage loop compensa los rejected (más fetches), pero **el costo extra por brief no debe superar +20%**. Si lo supera, considerar ajustar el threshold o mejorar las instrucciones del Researcher para extraer verbatim.

---

## Fase 3 — Estructural (~3-4 horas, mayor riesgo)

### A6. Recipes de metodología con freshness de evidencia

**Por qué:** un estimate que mezcla "fundada hace 5 años" + "revenue actual" produce rangos engañosos. Las recipes deben exigir frescura.

**Archivos:**
- `methodology/*.yaml`: añadir campo a cada recipe:
  ```yaml
  minimum_evidence_freshness_days: 365  # ajustar por recipe
  ```
  Sugerido: `net_worth_individual_v1` → 365, `consulting_firm_revenue_v1` → 365, `saas_revenue_v1` → 365, `public_disclosure_v1` → 730.
- `account_research/methodology/` loader: validar el campo al cargar.
- `account_research/agents/estimator.py` (~línea 101): en el dispatch, filtrar `signals_used` por `fetched_at >= now - freshness_days`. Si quedan menos del `minimum_signals` de la recipe, degradar `confidence` a "low" y **añadir caveat automático**: `"Some signals exceed the freshness window of N days."`

**Test:**
- Recipe con signal de 2 años → confidence baja a "low" + caveat aparece.
- Recipe con signals frescos → comportamiento sin cambio.

**Aceptación:**
- Caveat visible en el PDF cuando se gatilla.
- Ningún brief en regresión cambia confidence de "high" a "low" inesperadamente (si pasa, los datos de regresión ya estaban stale — registrar como hallazgo).

---

### A7. Async orchestration + paralelización

**⚠️ Mayor riesgo del plan. Hacer con buena cobertura de tests y en rama aislada. No mergear hasta validar regresión completa.**

**Por qué:** Researcher y la corroboración son independientes y comparten el mismo web cache. Hoy van en serie. Un brief tarda 3-6 min cuando podría tardar 1.5-2.

**Archivos:**
- `account_research/orchestrator.py`: convertir a `async def run(...)`. Usar `asyncio.gather()` para Researcher + corroboración. Estimator depende del Researcher (queda secuencial). Fact-Checker queda secuencial (depende del ledger completo) — paralelización incremental queda como **opcional v2**, no en este plan.
- `account_research/llm_client.py`: añadir variantes `complete_async()`.
- `account_research/tools/web.py`: añadir `asyncio.Lock` por URL hash para evitar race conditions en cache writes. Si ya usa `httpx`, migrar a `httpx.AsyncClient`. Escribir cache de forma atómica (tmp + rename).
- `account_research/cli.py` (~líneas 316-530): refactorizar para llamar `asyncio.run(orchestrator.run(...))` en vez de orquestar inline. **Subtarea: eliminar la duplicación de lógica entre CLI y orchestrator** (esto era un hallazgo separado del audit y se aprovecha el refactor).

**Test:**
- Integración: correr las 5 entidades canónicas. Resultado debe ser **idéntico** al baseline (mismo ledger, mismo brief). Solo cambia la latencia.
- Stress: correr 2 briefs en paralelo. Verificar que el web cache no se corrompe.

**Aceptación:**
- Latencia por brief baja ≥30%.
- Output idéntico al baseline.
- Sin race conditions detectadas en stress test.
- CLI ya no duplica lógica del orchestrator.

---

## Post-Plan A

1. Correr las 5 entidades de regresión.
2. Diff `docs/metrics-baseline.json` ↔ resultados finales. Documentar en `docs/metrics-phase-a.json`.
3. Reportar al usuario:
   - Ahorro real de costo (USD/brief).
   - Speedup real (s/brief).
   - Cambios en calidad (issue count del Reviewer, % verified, items rejected nuevos).
4. Cuando se apruebe, generar **Plan B** con los hallazgos relevantes restantes.

## Reglas para preguntar antes de actuar

Detente y pregunta al usuario si:
- Un cambio reduce la calidad del brief (más issues, menos items verified).
- Un test no se puede escribir sin mockear algo que parece estructural.
- Un file:line no corresponde a lo descrito y no encuentras el equivalente vía grep.
- El costo de regresión aumenta inesperadamente (p.ej. A5 sube costo >+20%).
- Una de las reglas no-negociables de `CLAUDE.md` parece estorbar — **nunca las saltes**.
