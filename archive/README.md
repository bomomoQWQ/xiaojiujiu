# Archive: the abandoned local model route

Everything in this directory belongs to a route the project **dropped on purpose**.
Nothing here is imported, packaged or tested by the active system. It is kept only
so the decision and its evidence remain inspectable.

## What was abandoned

A locally-run, fine-tuned ~2B generative model (target: `Qwen/Qwen3.5-2B`) that
was originally meant to do two jobs on the acting path:

1. **event appraisal** — turn the user's message into structured
   `direction / impact / activation / uncertainty / relation_signal / responsibility`;
2. **emotion explanation** — turn Runtime state into first-person psychological prose.

## Why

Two independent reasons, either of which would have been enough.

### 1. It was architecturally redundant (patch v0.2)

The host main LLM already sees the current message, the conversation, the character
persona and the injected Runtime state. It therefore already understands "what is
happening right now" better than a 2B model fed a compressed JSON view of it.
Running a second model *before* it duplicated that understanding and put a
generative model on the critical path of every reply.

The correct split, now implemented, is:

```text
acting layer      (main LLM)      : how do I respond, right now
persistent layer  (Runtime)       : what does this leave behind over days
```

### 2. It did not fit the target hardware

Measured on an i7-13700H with the Q4_K_M build, single appraisal request
(~430 prompt tokens, up to 96 output tokens):

| CPU budget | generation | one request | resident |
| --- | ---: | ---: | ---: |
| 8 threads | 20.8 tok/s | 4.6 s | 2050 MiB |
| 1 core (`taskset -c 0`) | 8.9 tok/s | 10.8 s | 2047 MiB |
| 0.5 core (cgroup quota, `nr_throttled` verified) | 3.7 tok/s | 25.7 s | 2047 MiB |

Quantization does not rescue it: Q3_K_M needs 1574 MiB and Q2_K 1294 MiB resident,
so even the smallest build cannot run on a 1 GB VPS, and none of them is fast
enough for anything a user waits on.

Threads do not rescue it either. Generation is memory-bandwidth bound and stops
scaling past ~8 threads; at 20 threads it *collapses* to 7.3 tok/s (measured with
`llama-bench`).

## What replaced it

* **Acting layer**: the host main LLM, as described above.
* **Persistent layer**: `companion_runtime.semantic` — a deterministic coarse
  settlement table. Explicit events (thanks, layoffs, bereavement, affection,
  conflict, stated need for space) settle into a direction and a coarse intensity
  band; anything ambiguous is recorded as `unresolved` and revisited later.
  No model is involved, and the ingest path stays at ~1 ms.
* **Optional accelerator**: `RemoteAPIProvider`, for low-frequency *deep cognition
  refresh* — reinterpreting old events that the rule layer deliberately declined to
  guess about. It defaults to `DisabledProvider`, and the Runtime is fully
  functional with nothing configured.

## What is still worth keeping

The **data and evaluation engineering** in `local_model_training/` remains a
correctness baseline for any future semantic provider: the JSON schemas, the
cross-field invariants (`EV01`–`EV07`, `EX01`–`EX09`), the coarse-settlement
taxonomy, and the evaluation metrics were all built from the design document and
are provider-independent. The *training* half is what is dead.

## Retired provider names

`build_provider` still recognises `local_cpu`, `local_gpu`, `local`, `cpu`, `gpu`
and `llama_cpp` **only to report that they were removed**. An operator who still
exports one gets `DisabledProvider` plus a warning naming the removal — never a
half-working local path and never a silent success.

## Reverting

This is a normal archived tree, not a deleted one. `git log` retains the full
history, and the removed Runtime modules (`local_llm.py`, `grammars/`) can be
restored from the commit that precedes the removal if the decision is ever
revisited.
