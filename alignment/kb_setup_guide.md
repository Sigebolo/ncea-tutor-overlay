# Knowledge Base setup guide (NCEA alignment)

DeepTutor supports multiple knowledge bases with per-KB RAG engines. Do **not**
build one big "Level 2 Physics" library — build one KB per standard so retrieval
never mixes AS 91171 mechanics with AS 91579 integration.

## KB naming convention

```
NZQA_<SUBJ><LEVEL>_<AS-CODE>_<ShortName>
```

Examples:

| KB name | Standard | Contents |
|---|---|---|
| `NZQA_P2_AS91171_Mechanics` | 91171 | standard PDF, spec, 2021–2025 exams + schedules + reports |
| `NZQA_C2_AS91164_Bonding` | 91164 | same pattern |
| `NZQA_C3_AS91579_Integration` | 91579 | same pattern |
| `NZQA_S3_AS91586_ProbDist` | 91586 | same pattern |

External standards in this workspace (exam drilling applies):

| Subject | External standards |
|---|---|
| Physics L2 | 91170 Waves, 91171 Mechanics, 91173 Electricity & EM |
| Chemistry L2 | 91164 Bonding/Structure/Energy, 91165 Organic, 91166 Reactivity |
| Calculus L3 | 91577 Complex Numbers, 91578 Diff Methods, 91579 Integration |
| Statistics L3 | 91584 Statistical Reports, 91586 Probability Distributions |

Internal (skill-practice only, no exam drilling): Physics 91172 Atomic &
Nuclear; Chemistry 91167 Quantitative Analysis; Statistics 91580 Time Series,
91581 Bivariate, 91582 Formal Inference.

(One KB per *standard*, not per year — years live as separate documents inside.)

## Which downloaded files go into each KB

From `nzqa_materials/{Subject}/AS{code}/`:

| File pattern | Priority | Why |
|---|---|---|
| `standard/as*.pdf` | 1 — highest | defines the evidence required |
| `specifications/*-spc-*.pdf` | 1 | exam rules for current year |
| `schedules/*-ass-*.pdf` | 2 | the A/M/E marking statements |
| `reports/*-report-*.pdf` | 2 | marker commentary — gold for E prep |
| `exams/*-exm-*.pdf` | 3 | question style + scaffolding |
| `exams/*-res-*.pdf` | 3 | resource booklets (formulae/data given in exam) |
| `exemplars/*-exp-*.pdf` | 4 | real graded scripts per grade |
| `exams/*-mex-*.pdf` | 4 | digital exam variants (skip if not needed) |

Tip: upload the 2025 and 2024 schedules/reports/exams first if upload time is
limited — most aligned to the current exam style. Add 2021–2023 next.

## Indexing settings

- Parsing: PDF engine with OCR/extraction (default) is fine — these are
  text-based PDFs.
- Chunking: moderate size with some overlap; schedules have tables that
  should stay intact, so avoid tiny chunks.
- After indexing, ask the tutor: "Quote the Excellence descriptors for
  AS 91171 from the 2024 assessment schedule" — if it answers with the right
  rows and cites the file, the KB is properly grounded.

## Per-student isolation

- Create one account per child/student (learner accounts are supported).
- Each student gets their own Space; give the shared KBs to each Space so
  memory and chat history stay separate per student.
