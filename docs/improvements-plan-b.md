# Improvements Plan B — Follow-ups from Plan A

> **Para Claude Code:** este archivo es el plan de trabajo que sigue a `improvements-plan.md` (Plan A). Léelo entero antes de empezar. Las convenciones del proyecto están en `CLAUDE.md`. Si algo de este plan contradice `CLAUDE.md`, gana `CLAUDE.md` y pregunta al usuario.

## Contexto

Plan A (A1–A7) está mergeado a `main` (commits `2904266…3c0e08c`). 348 tests verde. Hallazgos resumidos en `docs/metrics-phase-1.json` + commit messages.

Plan B captura **tres categorías** de trabajo derivado de Plan A:

1. **Findings durante implementación** — bugs concretos descubiertos al medir.
2. **Deferred de Plan A** — items que el plan listaba pero quedaron fuera de scope (con justificación) o que se descubrieron como falsas premisas.
3. **Plan A surface gaps** — cosas que A1–A7 expusieron pero no resolvieron.

**Reglas no-negociables de `CLAUDE.md` siguen vigentes** — ningún cambio puede violarlas.

## Findings registrados durante Plan A

| # | Finding | Origen | Severidad |
|---|---|---|---|
| F1 | `$PARAMETER_VALUE` wrap variant tumbó el Author en el baseline | Hotfix `8a07457` (Phase 1) | crítica |
| F2 | Reviewer rigor keeps rejecting Guillermo Calderón cada iter → escalado a `human_review_needed` con costo de 3× Author | Métricas post-Phase-1 | alta |
| F3 | Author hace 2 LLM calls por iter (primer emit + semantic re-pass), inflando ~50% del costo de revisión | `metrics-phase-1.json` | alta |
| F4 | Estimator recipe routing por industria pendiente; consulting recipe sobre Stripe-class produce 100× low | Memory `estimator-recipe-routing.md` | alta |
| F5 | Vision fallback no dispara sin poppler instalado; el kill-switch funciona pero perdemos catch de caveats | Logs post-Phase-1 (`Reviewer: vision fallback unavailable`) | media |
| F6 | Embeddings cache de A4 es process-local; refine flow (Streamlit) y CLI no comparten | Comentario en `citation_validator.py` | media |
| F7 | `rejected` (A5) se cuenta en `unverifiable` en `LedgerReport` — pierde granularidad en Library UI | `_classify` en `fact_checker.py` | baja |
| F8 | `direct_web_fetch_async` existe pero Fact-Checker sigue usando el path sync | A7 implementation | baja |
| F9 | A1 cache hit es 0 en iter 1 (cache_write) — costos no bajan en single-shot briefs | `metrics-phase-1.json` per_agent.author | informativa |
| F10 | A5 puede rechazar Tier-1 items legítimos cuyo HTML está mal rendido (no JS) → cobertura cae | Diseño A5 + tests | media |

## Deferred desde Plan A

| # | Plan A item | Razón |
|---|---|---|
| D1 | A7 full async (orchestrator + LLMClient.complete_async) | Premisa falsa: Researcher∥corroboración son secuenciales en la realidad. Ganancia real ≈ 0 una vez excluyes Author/Reviewer (estrictamente secuencial por loop). |
| D2 | A7 subtask: eliminar duplicación CLI ↔ orchestrator | Refactor estructural sin ganancia funcional; postergado a refactoring puro cuando haya causa real. |

---

## Fase B1 — Precisión (~1.5 horas, mayor impacto en calidad)

### B1. Estimator recipe routing por industria

**Por qué:** memory `estimator-recipe-routing.md` describe el bug: consulting recipe aplicada a Stripe-class produjo estimación 100× low. Las recipes self-gate por signals pero no por entity industry. Sin esto, A6 freshness no salva: una recipe equivocada con signals frescos produce confianza alta y rango engañoso.

**Archivos:**
- `account_research/methodology/loader.py` (~línea 90): añadir campo `applies_to_industries: list[str] = []` al `Recipe`.
- Cada YAML: añadir lista explícita (`consulting_firm_revenue_v1` → `[professional_services, consulting]`, `saas_revenue_v1` → `[saas, software]`, `public_disclosure_v1` → wildcard `["*"]`, `net_worth_individual_v1` → `[]` solo para personas).
- `account_research/agents/estimator.py` (donde dispatcha recipes): filtrar recipes por industry antes de ejecutar. Si el entity industry no matchea, skip recipe.
- Industry detection: usar la primera categoría del brief o, mejor, el `industry_classifier` que ya existe (`account_research/agents/industry_classifier.py`).

**Tests:**
- Recipe consulting + entity industry=saas → recipe NO se ejecuta.
- Recipe public_disclosure + cualquier industry → se ejecuta (wildcard).
- Stripe-like ledger + industry=fintech → solo recipes saas/public_disclosure se intentan.

**Aceptación:**
- Stripe regression entity: solo recipes apropiadas corren.
- No degrada cobertura en LATAM consulting briefs (BWPM, Guillermo Calderón) — sus industries quedan en consulting/services.
- Test de regresión sobre las 3 recipes-conocidas-buggy con ledger híbrido.

---

### B2. Defense in depth: regex `$PARAMETER_*` wrap detection

**Por qué:** F1. Dos variantes ya vistas (`$PARAMETER_NAME`, `$PARAMETER_VALUE`). El allowlist actual requiere añadir cada nueva variante. Una vez Claude emite `$PARAMETER_TYPE` el Author crashea de nuevo.

**Archivos:**
- `account_research/llm_client.py` (~línea 469): reemplazar el for-loop de placeholders explícitos por un regex que matchee `^\$[A-Z_]+$` cuando el dict es single-key.
- Mantener log warning incluyendo el placeholder concreto para detectar nuevas variantes.

**Tests:**
- `$PARAMETER_NAME`, `$PARAMETER_VALUE`, `$PARAMETER_TYPE`, `$INPUT`, `$ARGS` → todos se desenvuelven.
- `$payment` (lowercase) NO se desenvuelve (real campo posible).
- `parameter` (sin `$`) sigue funcionando por la lista existente.

**Aceptación:**
- Tests pasan.
- Memory `claude-parameter-name-wrap.md` actualizado para reflejar el regex catch-all.
- Sin regresión en los 16 tests existentes de `test_llm_unwrap.py`.

---

### B3. Surface `rejected` items separadamente en la Library UI

**Por qué:** F7. A5 introdujo el status `rejected` para quotes que fallan tanto fuzzy como LCS. Hoy se cuentan en `unverifiable` para mantener `LedgerReport` shape-stable, pero la Library UI pierde visibilidad de cuántos items fueron rejected vs unverifiable — métrica útil para diagnosticar Researcher quality.

**Archivos:**
- `account_research/schemas/evidence.py`: añadir `rejected: int = Field(ge=0, default=0)` a `LedgerReport`.
- `account_research/agents/fact_checker.py` (~línea 296): contar separadamente; mantener `unverifiable` solo para genuine unverifiable (no rejected).
- `pages/1_Library.py`: columna nueva en el table view (`Rejected`) + tooltip explaining the difference.
- `account_research/quality.py`: incluir `rejected_count` en metrics output.

**Tests:**
- `LedgerReport.rejected` cuenta correctamente.
- `verification_rate` no cambia (solo cuenta verified/total).
- Library UI test no rompe (puede ser smoke por subprocess; usar `tests/integration/` si necesario).

**Aceptación:**
- UI muestra el conteo de rejected.
- Migración aditiva en SQLite (`db._run_additive_migrations`) no requerida — `rejected` es derivado en runtime de los items.

---

## Fase B2 — Costo / cobertura (~2 horas, ahorra dinero y tiempo)

### B4. Wire async batch fetch en Fact-Checker

**Por qué:** F8. `direct_web_fetch_async` existe (A7) pero Fact-Checker no lo usa. En runs con cache cold (primera vez sobre una entidad nueva o tras `--no-cache`), FC fetchea N URLs serialmente. Con 30 URLs × 2s = 60s; en paralelo con concurrency=8 → ~10s.

**Archivos:**
- `account_research/agents/fact_checker.py` (`run` method): refactorizar el for-loop a 2 fases:
  1. **Fetch phase**: `asyncio.run(_batch_fetch(items))` que llama `direct_web_fetch_async` para cada URL con `asyncio.Semaphore(8)`. JS-heavy domains saltan a Haiku (también puede ser async pero por simplicidad seqüencial para no quemar Anthropic rate limit).
  2. **Classify phase**: serial sobre los results.
- Mantener interfaz pública `FactCheckerAgent.run()` sync para no romper callers.

**Tests:**
- Mock `direct_web_fetch_async` y verificar que se llama una vez por URL.
- Verificar que `result.report` shape es idéntico al path sync.
- Mismo regression-test set que test_fact_checker.py pasa por ambos paths.

**Aceptación:**
- Tiempo wall-clock de FC en regression baja ≥30% en runs cold-cache.
- Output bit-idéntico al path sync (mismo set verified/unverifiable/rejected/source_dead).
- Sin race conditions: cache test ya cubre concurrent writes (A7).

---

### B5. Skip Author semantic re-pass cuando primer emit pasó el validator

**Por qué:** F3. Author corre el semantic validator después de cada emit; si `compute_weak_citations` retorna no-vacío, hace una segunda llamada Opus (~$0.85). En la realidad muchos primeros emits pasan limpio — y todavía pagamos overhead de la segunda llamada en iter 2+.

**Archivos:**
- `account_research/agents/author.py:165-194`: re-leer la lógica del bloque de re-author. Actualmente:
  ```python
  weak = compute_weak_citations(brief, payload.ledger)
  if weak:
      # re-author
  ```
- Cambio: solo hacer re-author si **NUEVOS** weak citations aparecieron respecto a `payload.previous_weak_citations`. Esto evita re-author cuando Author ya está en revision-mode y el validator está reportando los mismos flags que el iter anterior reportó.

**Tests:**
- Iter 1: primer emit con weak → re-author dispara (caso actual).
- Iter 1: primer emit limpio → re-author NO dispara (caso actual).
- Iter 2: primer emit con weak iguales a `previous_weak_citations` → re-author NO dispara (caso NUEVO).
- Iter 2: primer emit con weak NUEVOS → re-author dispara.

**Aceptación:**
- En el regression run que escaló a 3 iters (Guillermo), conteo de Author calls baja de 6 → 4 (3 emits + 1 re-pass) o menos.
- Quality: no degrada `confidence_score` ni `citation_backing` (medible vs baseline pre-B5).
- Si confidence baja >5pp en regression, revertir.

---

### B6. Embedding cache persistido a SQLite

**Por qué:** F6. Cada proceso Python (CLI run, refine button en Streamlit) tiene su propio `_QUOTE_EMBEDDING_CACHE` dict. Una corrida de regresión + un refine en la UI re-embedden los mismos quotes — Voyage call duplicada.

**Archivos:**
- `account_research/db.py`: nueva tabla `quote_embeddings` (`quote_hash TEXT PK, vector_json TEXT, model TEXT, created_at TIMESTAMP`).
- `account_research/agents/citation_validator.py`: `_QUOTE_EMBEDDING_CACHE` becomes a thin write-through layer over the SQLite table. On import, no eager load — lookup-on-demand keeps startup fast.
- Migration: aditivo (`_run_additive_migrations` ya existe del A5).

**Tests:**
- Embed quote A en proceso 1, exit. Process 2 (fresh) lookup → hit on SQLite, sin llamar Voyage.
- TTL: ¿se invalida después de N días? Por ahora no (los embeddings son deterministas; modelo nuevo cambia hash key via `model` columna).
- Concurrent writes safe (SQLite WAL ya está activado).

**Aceptación:**
- Refine call subsequent a un Run completo del mismo entity: 0 Voyage calls.
- Costo Voyage en regression suite (5 entidades × 3 iters) baja en ≥40% del actual.

---

## Fase B3 — Observabilidad / postura (~1 hora)

### B7. Vision diagnostics en la Run page

**Por qué:** F5. En el regression run el log dice "vision fallback unavailable (poppler not in PATH)" pero la UI no lo refleja — el operador asume vision está disparando. Si vision no funcionó, los rigor checks textuales se vuelven el único catch y bug los caveats invisibles.

**Archivos:**
- `account_research/agents/reviewer.py`: el log existente queda. Añadir además a `ReviewerReport` un campo `vision_used: bool` que refleja si `_rasterize_pdf` produjo al menos un block (no solo si se intentó).
- `pages/2_Run.py`: si `vision_used=False` en cualquier iteración, mostrar un warning banner: "Vision unavailable — install poppler for full reviewer coverage".
- Documentar en README cómo instalar poppler en Windows / Mac.

**Tests:**
- `vision_used=True` cuando `_rasterize_pdf` yields ≥1 image.
- `vision_used=False` cuando exception capturada (poppler missing).

**Aceptación:**
- UI surface lo que el log dice.
- README actualizado.

---

### B8. Costs page: surface cache_read / cache_creation tokens

**Por qué:** F9. A1 logueamos cache tokens en el trace, pero `pages/3_Costs.py` solo muestra `input_tokens + output_tokens`. Operador no ve si caching está disparando.

**Archivos:**
- `pages/3_Costs.py`: añadir columnas `Cache Read` y `Cache Created` al breakdown. Recomputar `usd` con las pricing rates de cache (10% para read, 125% para creation).

**Tests:**
- Smoke: cargar un trace con cache tokens y verificar que el UI no rompe.
- Unit test puro sobre la función `_cost` cuando incluye cache fields.

**Aceptación:**
- Costs page muestra savings reales de cache en USD.
- Diff per-agent: cache_read tokens visible.

---

### B9. Confidence score incluye `claim_similarity_score` (A5)

**Por qué:** A5 introdujo `Verification.claim_similarity_score` pero `confidence_score` en `quality.py` solo usa el conteo verified. Items verified con `claim_similarity_score < 0.95` deberían pesar menos — son "verified pero apenas".

**Archivos:**
- `account_research/quality.py:citation_backing`: ya cuenta evidencias verified como 1.0 cada una. Cambiar a peso proporcional a `claim_similarity_score` cuando esté presente; default 1.0 cuando es None (compatibilidad con rows pre-A5).

**Tests:**
- 5 items verified con similarity=1.0 → citation_backing = 100.
- 5 items verified con similarity=0.85 → citation_backing ≈ 85.
- Mix → promedio ponderado.

**Aceptación:**
- Confidence score más sensible a "verified-pero-apenas".
- No degrada test_quality.py existentes — viejo tests no tienen `claim_similarity_score` (None → peso 1.0).

---

## Cosas que NO entran a Plan B

| # | Item | Razón |
|---|---|---|
| X1 | Orchestrator full async (A7 original) | Premisa falsa documentada en commit A7. Sin ganancia real. |
| X2 | LLMClient.complete_async | Demandado por orchestrator async; sin orchestrator async no aporta. |
| X3 | CLI ↔ orchestrator dedup | Refactor sin causa funcional; postergar a cuando haya una. |
| X4 | Author → Designer → Reviewer paralelización por iteración | Estrictamente secuencial por el revision loop. |

## Workflow

1. **Rama por Fase** (`improvements/plan-b-phase-1`, `phase-2`, `phase-3`).
2. **Commits atómicos por item** (B1, B2, ..., B9). Mensaje: `[B<n>] <descripción corta>`.
3. **Tests verdes antes de marcar completa** — TDD light como en Plan A.
4. **Cobertura de regresión al final de cada fase**: correr Stripe (B1 prueba directamente el routing) + Guillermo Calderón (B5 prueba el ahorro de re-pass). Diff contra `docs/metrics-baseline.json` y `docs/metrics-phase-1.json`.
5. **Ambigüedad → preguntar.** Si una premisa del plan no aplica al código actual, preguntar en lugar de improvisar. Plan A descubrió esto en A2 (vision ya always-on) y A7 (corroboración no paralelizable).

## Métricas a capturar

Al final de cada fase, repetir el script `scripts/phase_diff.py` con `--since <utc-start>` y comparar:

- **Cost** total y por agente (delta esperado: B5 ≤ -25%, B4 ≤ -10% wall, B6 ≤ -5% Voyage).
- **Latency** wall-clock por brief.
- **Token mix**: prompt / completion / cache_read / cache_creation.
- **Reviewer**: iteraciones promedio, final status, issue count.
- **Ledger**: verified / unverifiable / **rejected** / source_dead (B3 surface esto).
- **Confidence score** promedio (B9 lo afecta directamente).
- **Voyage calls** y embeddings cache hit rate (B6).

## Reglas para preguntar antes de actuar

Detente y pregunta al usuario si:
- Un cambio reduce calidad del brief (más rejected, menos verified, peor confidence score). Especialmente B5 (re-pass skip).
- El regression suite baja en cualquier metric sobre las 5 entidades.
- B1 falsea: si la industry detection cae a "unknown" para una entidad, ¿qué recipes corren? Probablemente todas, fallback explícito.
- B6 SQLite migration falla en producción (otros readers).
- Una premisa del plan no aplica al código actual — repetir lección de Plan A.

## Aceptación global del Plan B

Al cerrar las 3 fases:
- ≥348 tests verde (no perder ninguno).
- Cost por brief baja ≥20% combinado (caching + B5 + B6).
- Latencia baja ≥10% (B4).
- Confidence score mediana sobre las 5 entidades sube ≥3 puntos (B1 + B9).
- Documentar resultados en `docs/metrics-plan-b.json` y reportar al usuario.
