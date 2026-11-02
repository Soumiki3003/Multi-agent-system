# KnowledgeGraph

KnowledgeGraph is a Flask tutoring platform for a CTF / binary-analysis course built around `angr_ctf`. Instructors upload course documents, an LLM turns them into a knowledge graph in Neo4j, and students work through challenges with a Socratic AI tutor. The app also runs the study itself: surveys unlock each challenge, and instructors get progress dashboards and chat transcripts.

**Jump to:** [Quick map](#quick-map) · [Agents](#agents) · [Document-to-graph extraction](#document-to-graph-extraction) · [Answering pipeline](#answering-pipeline) · [How the answer reaches the student](#how-the-answer-reaches-the-student) · [All prompts](#all-prompts) · [Frontend](#frontend) · [Data model](#data-model) · [Routes](#routes) · [Setup](#setup) · [Known issues](#known-issues)

---

## Quick map

| I want to find… | Where it lives |
|---|---|
| App entry point and blueprint registration | `main.py` |
| Agent, model and service wiring | `app/containers.py` |
| Model names, index names, Neo4j settings | `config.yaml` (values come from `.env`) |
| Document extraction prompts | `app/templates/prompts/knowledge_system_prompt.j2`, `knowledge_user_prompt.j2` |
| Variables passed into the extraction system prompt | `app/prompts.py` |
| Document parsing, OCR and chunking | `app/services/file.py` |
| Knowledge graph build and storage | `app/services/knowledge.py` |
| Tutor logic and its answer, hint and summary prompts | `app/services/supervisor_agent.py` |
| Neo4j driver, retriever and GraphRAG setup | `app/gateways/neo4j.py` |
| Verified per-level solving guide | `angr_summary.md`, loaded by `app/services/ctf_reference.py` |
| Chat orchestration (save, answer, save) | `CourseController.chat_send` in `app/controllers/course.py` |
| Chat storage | `app/services/chat.py` |
| HTTP routes | `app/views/` |
| Frontend templates | `app/templates/web/` |
| Challenge order and unlocking | `app/challenges.py` |
| Survey questions; survey PDF and email | `app/surveys/data.py`; `app/surveys/service.py` |
| Instructor analytics; transcript PDF | `app/services/dashboard.py`; `app/services/chat_export.py` |
| CTF course seed data and script | `chunks.json`, `seed_ctf.py` |
| Demo course seed script | `seed_demo.py` |

### Tech stack

Python 3.12+, Flask, `dependency-injector`, Neo4j with `neo4j-graphrag`, and `pydantic-ai`. The frontend is server-rendered Jinja2 with HTMX, Tailwind, DaisyUI and `marked`. PDF and OCR support comes from PyMuPDF, Poppler and Tesseract; `reportlab` generates PDFs.

### Request flow

```text
Browser (HTMX) → Views (app/views/) → Controllers (app/controllers/)
               → Services (app/services/) → Gateways (app/gateways/) → Neo4j / LLM
```

---

## Agents

The "agents" are not a set of autonomous, tool-calling agents. The app configures **three `pydantic-ai` `Agent` objects** in the `AI` container (`app/containers.py`), plus one embedder. A plain Python service, `SupervisorAgentService`, decides when to call them.

| Agent | Model setting | Job | Prompt location | Used at runtime? |
|---|---|---|---|---|
| `knowledge_file_agent` | `KNOWLEDGE_FILE_MODEL` | Extracts a knowledge graph from uploaded documents | `knowledge_system_prompt.j2` (system), `knowledge_user_prompt.j2` (user) | Yes, on upload |
| `default_agent` | `DEFAULT_MODEL` | Writes the student's answer, the hint and the history summary | Built inline in `app/services/supervisor_agent.py`. It has no system prompt | Yes, on every answered question |
| `graph_agent` | `GRAPH_MODEL` | Supplies the model name and settings for GraphRAG's LLM wrapper (`Neo4jAgent`) | `_RAG_SYSTEM_INSTRUCTIONS` in `app/gateways/neo4j.py:44` | **No.** Its prompt is never sent (see below) |

Related components:

| Component | What it is | Where |
|---|---|---|
| `SupervisorAgentService` | The tutor's orchestrator. It is ordinary Python, not an LLM | `app/services/supervisor_agent.py` |
| `default_embedder` (`EMBEDDER_MODEL`) | Embeds content chunks and student questions | `app/containers.py:82` |
| `content_chunk_graphrag` | Vector retriever over `ContentChunk` nodes; finds course material | `app/containers.py:158` |
| `trajectory_graphrag` | Vector retriever over `UserTrajectory` nodes; detects repeated questions | `app/containers.py:136` |

Three details are easy to misread in the code:

- **"Hint agent" and "rewrite agent" are one object.** The container passes `ai.default_agent` as both `hint_agent` and `rewrite_agent` (`app/containers.py:222-223`). They differ only in the prompt they receive.
- **The GraphRAG draft step no longer runs.** The tutor calls `rag.retriever.search()` directly rather than `GraphRAG.search()` (`app/services/supervisor_agent.py:263`), so `graph_agent`'s prompt is never used. Comments in `app/containers.py:101` and `app/gateways/neo4j.py:39` still describe the old draft step.
- **Prompts can't be edited in the app.** The **Agent Prompts** tab under course settings is a "Work in Progress" placeholder. To change a prompt, edit the files listed above.

### Agent settings

| Setting | `knowledge_file_agent` | `default_agent` | `graph_agent` |
|---|---|---|---|
| Max output tokens | Model default | 600 | 600 |
| Timeout per request | 30 s | 30 s | 30 s |
| Retries | 3 | 3 | 3 |
| Structured output | `models.RootKnowledge` | Plain text | Not applicable |

---

## Document-to-graph extraction

Upload starts on the **Upload Materials** tab in course settings and runs synchronously inside the HTTP request.

```text
POST /course/<id>/upload                                    app/views/course.py:478
  └─ CourseController.upload_to_course                      saves to app/static/uploads/
      └─ KnowledgeService.add_document_to_course            app/services/knowledge.py:467
          ├─ FileService.extract_textual_content            PDF: PyMuPDF · PPTX: python-pptx · HTML: BeautifulSoup
          ├─ FileService.extract_visual_content             Tesseract OCR on PDF pages and PPTX images
          ├─ For each batch of 20 pages:
          │    render knowledge_user_prompt.j2 with the pages and the graph so far
          │    knowledge_file_agent.run_sync(output_type=RootKnowledge)
          │    the returned graph becomes the input to the next batch
          ├─ FileService.chunk_textual_content              one chunk per page, plus one per paragraph
          └─ One Neo4j transaction:
               create nodes and HAS_CHILD edges under the course root
               add the file to the root's sources
               embed chunks and store ContentChunk nodes (HAS_CONTENT)
```

### Graph shape

```text
RootKnowledge (the course)
  └─ HAS_CHILD → ConceptualKnowledge     definition, prerequisites, misconceptions, connections
        └─ HAS_CHILD → AssessmentKnowledge   quizzes and evaluation criteria (leaf node)
        └─ HAS_CHILD → ProceduralKnowledge   code_snippet, hint, common_errors, percent_done
              └─ HAS_CHILD → next ProceduralKnowledge step (a linear chain)
```

Concept-to-concept relationships are `PREREQUISITE_FOR`, `DEPENDS_ON`, `EXTENDS_TO`, `GENERALIZES_TO`, `USES_INIT_FROM`, `IMPLEMENTED_BY` and `ENABLES`. Node schemas are in `app/models/knowledge.py`.

### Two ways to populate a course

| | Upload a document | `seed_ctf.py` |
|---|---|---|
| Graph source | LLM extraction | Hand-curated `chunks.json`; no LLM extraction |
| Chunk source | Raw page and paragraph text | Generated from node fields such as definitions, hints and code |
| Step-chunk format | Not applicable | `Step — Hint: … — Code: …` |

### What the tutor actually reads

**Answers are retrieved by vector search over `ContentChunk` nodes. The tutor doesn't traverse the graph.** The conceptual and procedural nodes and their relationships power the settings graph editor and the instructor dashboards, but `HAS_CHILD` and `PREREQUISITE_FOR` edges are never followed when answering. For uploaded documents, this means the LLM-extracted graph doesn't directly feed student answers; the raw document chunks do.

---

## Answering pipeline

`SupervisorAgentService.retrieve_context` (`app/services/supervisor_agent.py:675`) handles each question in this order:

| # | Step | Uses an LLM? | Details |
|---|---|---|---|
| 1 | Survey guard | No | Keywords or at least two multiple-choice lines (`A)`, `B.`) return a fixed refusal and stop |
| 2 | Retrieve chunks | Embedding only | Top 5 `ContentChunk` nodes by vector similarity |
| 3 | Confidence check | No | No results, or a top score under 0.6, returns the fallback message and stops |
| 4 | Classify intent | No | Keyword match: definition, procedural, troubleshooting, example request or context request |
| 5 | Re-rank | No | Sorts by intent type match, difficulty, teaching order, then score |
| 6 | Count repeats | Embedding only | Earlier questions from this student in this course and challenge: exact match, or similarity of at least 0.70 |
| 7 | Prepare history | Sometimes | Up to 4 prior messages go in verbatim; beyond that, older ones are summarized in up to 60 words and the latest 2 stay verbatim |
| 8 | Generate hint | Sometimes | Only when the repeat count is at least 2 |
| 9 | Load ground truth | No | The matching level section of `angr_summary.md`; stage N maps to level N−1 |
| 10 | Generate answer | **Yes** | Combines scaffolding level, rules, chunks, ground truth, history and the question |
| 11 | Log trajectory | Embedding only | Saves a `UserTrajectory` node for repeat detection and analytics |

### Scaffolding levels

The repeat count from step 6 determines how much the tutor reveals:

| Repeat count | Level | Tutor behaviour | Solution code in the guide? |
|---|---|---|---|
| 0 | First ask | Explains the concept and asks a guiding question; no complete code | Removed before sending |
| 1 | Second ask | Reveals one step with its exact code | Included |
| 2 or more | Repeated ask | Names the specific fix and gives only the lines that change; a hint is also generated | Included |

### Source priority

When both are available, the prompt tells the model that the ground-truth guide outranks retrieved course material, and to trust the guide if they conflict on concrete details such as addresses, names, code or steps.

### Messages that skip the LLM

| Situation | Student sees | Trajectory logged? |
|---|---|---|
| Looks like a survey or quiz question | `SURVEY_DECLINE_RESPONSE` | No |
| No retrieval results or low confidence | `RESPONSE_FALLBACK` | No |
| Exception inside the tutor | `RESPONSE_FALLBACK` | No |
| Exception in the controller | "An error occurred while processing your message." | No |

---

## How the answer reaches the student

The answer is delivered as **one complete HTML fragment over HTMX**. There is no streaming and no WebSocket, so the student sees a spinner until the entire pipeline finishes.

```text
1. Student presses Enter                     app/templates/web/course/chat.html
2. HTMX POSTs the form                       /course/<id>/challenge/<stage>/send
3. View calls the controller                 app/views/course.py:212
4. CourseController.chat_send                app/controllers/course.py:140
     a. load up to 20 prior messages for this challenge
     b. save the student's ChatMessage
     c. SupervisorAgentService.retrieve_context → answer and optional hint
     d. save the assistant's ChatMessage
5. View renders chat_message.html            student bubble, assistant bubble, optional hint bubble
6. HTMX appends it to #chat-messages         hx-swap="beforeend"
7. base.html converts markdown to HTML       marked.parse on .md-bubble, after htmx:afterSwap
```

The answer returned to the student is the unmodified, whitespace-trimmed output of the answering prompt. No later step checks or rewrites it.

On a page reload, `chat.html` re-renders saved `ChatMessage` nodes for that challenge and `base.html` converts their markdown again.

### Hints

A generated hint appears in two places: as a yellow **💡 Hint** bubble under the answer, and in the lightbulb modal in the chat header. The badge on that button polls for new hints every 10 seconds.

Generated hints are **approved automatically**: `UserTrajectory` sets their status to `approved` (`app/models/user.py:73`), so they never wait in the instructor's queue. Instructors can also send manual hints to one student or all students. In the modal, a hint expires 24 hours after it is first read.

---

## All prompts

Everything below was captured from the current code. Text in `<angle brackets>` marks a value filled in at runtime.

### Answer prompt

**Source:** `__generate_response`, `app/services/supervisor_agent.py:478` · **Agent:** `default_agent`

The prompt has the same structure on every ask; only the scaffolding block changes.

<details>
<summary>Complete answer prompt (first ask, with ground truth)</summary>

````text
You are a Socratic teaching assistant for a CTF/binary analysis course.

## Scaffolding Level: FIRST ASK
The student is asking about this topic for the first time.
- Explain the CONCEPT behind the answer — what it is and why it matters.
- Give a high-level direction without revealing specific values, addresses, or full code.
- Ask a guiding question to prompt the student to think further.
- Do NOT show complete code snippets yet.
Example style: 'The bitvector size needs to match the input field size. If scanf reads an 8-byte string, how many bits would that be?'

General rules (always apply):
- Scope: this CTF/angr course only. Off-topic or unrelated-task requests → decline, redirect to the challenge.
- Never answer survey/quiz/knowledge-test questions, even paraphrased — decline and point them to answer independently.
- NEVER invent code, function names, or APIs. Only use code from the course material above.
- Never mention: nodes, graph, knowledge graph, concept node, procedural node, assessment node, retrieval, scores, labels, edges, PREREQUISITE_FOR, DEPENDS_ON, EXTENDS_TO, ENABLES, or any other graph relationship names.
- Never reference internal system structure or metadata.
- Write as if you are a human tutor speaking directly to the student.
- Do NOT start your response with phrases like 'Here is a rewritten answer' or any similar meta-commentary. Just give the answer directly.
- This is a hint-driven tutor, not a tutorial site: give the shortest answer that resolves the specific question, never a complete write-up. No preamble, no restating the question, no repeating context the student already has.
- Never structure your answer with multiple headers/sections (no 'Complete Fix', 'What Each Line Does', 'How to Get Your Addresses', 'Why It Was Slow', 'Next Steps', etc.) and never re-paste the student's entire script back at them — at most one short, targeted code snippet.
- Target a few sentences. If you find yourself writing more than that plus one snippet, you are over-explaining — cut it down to the one thing that actually answers the question.
- Before answering, compare the course material against the ground-truth reference below. If they agree, use either. If they conflict on any concrete detail (addresses, register/variable names, function names, code, or steps), or the course material is missing something the ground-truth reference covers, trust the ground-truth reference and frame your answer using it instead — do not mix in the conflicting course-material detail.


## Course material retrieved from the knowledge graph (ONLY use facts and code from here — do NOT invent anything):
<retrieved chunk 1>
---
<retrieved chunk 2>

## Ground-truth reference for this challenge (complete and verified — this OUTRANKS the course material above):
<angr_summary.md section for this level>

<history context>

Student question: <student question>
````

</details>

<details>
<summary>Scaffolding block: second ask</summary>

````text
## Scaffolding Level: SECOND ASK
The student has asked about this before and may be stuck.
- Acknowledge their prior attempt naturally.
- Reveal ONE specific step from the course material with its exact code snippet.
- Explain what that code does so they understand, not just copy.
- Still withhold the complete end-to-end solution.
Example style: 'Since each password is 8 bytes, the bitvector should be 8*8 = 64 bits: claripy.BVS("password0", 8*8). Can you apply this to the other passwords?'
````

</details>

<details>
<summary>Scaffolding block: repeated ask</summary>

````text
## Scaffolding Level: REPEATED ASK
The student has asked about this multiple times and is clearly stuck.
- Be direct: name the specific fix, not just a conceptual nudge.
- If code is needed, give ONLY the line(s) that need to change — reproduced EXACTLY as they appear in the course material above, do NOT paraphrase, simplify, or modify. Do NOT rewrite or restate their entire script.
- Do not walk through the whole file line by line — one short note on why the fix works is enough.
- Encourage them to run it and come back if they hit another issue.

````

</details>

Without a ground-truth section for the level, the "Before answering, compare…" rule and the ground-truth block are both omitted.

### Hint prompt

**Source:** `__generate_hint`, `app/services/supervisor_agent.py:359` · **Agent:** `default_agent`. The question and history context are each capped at 400 characters.

````text
In 1-2 short sentences, give an encouraging hint to help the student progress on: '<student question>'. Focus on conceptual reinforcement rather than giving the answer directly. Be brief — no preamble, no restating the question. Context nodes: ['<node 1>', '<node 2>', '<node 3>']<history context>
````

### History summary prompt

**Source:** `__summarize_history`, `app/services/supervisor_agent.py:185` · **Agent:** `default_agent`

````text
Summarize this tutoring conversation in at most 60 words: what topics were covered, what specific facts/code/values were already revealed to the student, and where they seem stuck. Plain prose, no preamble.

Student: <message 1>
Tutor: <message 2>
Student: <message 3>
````

### Dormant GraphRAG prompt

**Source:** `_RAG_SYSTEM_INSTRUCTIONS`, `app/gateways/neo4j.py:44` · **Agent:** `graph_agent`. This prompt is configured but never sent.

````text
Answer the user's question using ONLY the provided context — do not invent facts or code. This is an internal draft that will be rewritten before a student ever sees it, so prioritize brevity over completeness: a few plain sentences, plus one short code snippet only if truly necessary. No markdown headers, no emoji, no multiple sections.
````

### Fixed responses

**Source:** `app/services/supervisor_agent.py:63`. These don't use an LLM.

````text
RESPONSE_FALLBACK:
I couldn't find relevant information to answer your question. Please try rephrasing.

SURVEY_DECLINE_RESPONSE:
That looks like a survey, quiz, or knowledge-test question rather than a question about your current challenge — I can't help answer those, since they're meant to reflect your own understanding. Happy to help if you have a genuine question about the challenge itself!
````

### Document extraction: system prompt

**Source:** `app/templates/prompts/knowledge_system_prompt.j2`, with variables from `app/prompts.py` · **Agent:** `knowledge_file_agent`

<details>
<summary>Full template</summary>

````jinja
You are an expert educational data modeler and AI architect specializing in automated knowledge graph construction for multi-agent learning systems. Your goal is to process instructional or challenge-based documents (e.g., CTF tutorials, lab guides, programming assignments, course handouts) and construct a unified Knowledge Graph (KG) following a strict hierarchical structure.

## Core Architecture

### Knowledge Graph Hierarchy
```
RootKnowledge (type: "root", name: "Central node")
  └─> children: [ConceptualKnowledge]
        └─> children: [ProceduralKnowledge | AssessmentKnowledge]
              └─> child: ProceduralKnowledge (single next step)
                    └─> child: ProceduralKnowledge
                          └─> child: null (final step)
```

### Knowledge Types & Rules

**1. RootKnowledge**
- The single top-level node
- `type`: "root"
- `name`: "Central node"
- `children`: Array of **ConceptualKnowledge only**

**2. ConceptualKnowledge** (Definitions, theories, principles)
- `type`: "conceptual"
- `bloom_level`: "{% for lvl in bloom_levels.conceptual %}{{ lvl }}{{ " | " if not loop.last }}{% endfor %}"
- `children`: Array of **ProceduralKnowledge** OR **AssessmentKnowledge** (can mix both types)
- `connections`: Array of connections to OTHER ConceptualKnowledge nodes
- Contains: definitions, prerequisites, misconceptions, learning objectives

**3. ProceduralKnowledge** (Steps, procedures, how-to instructions)
- `type`: "procedural"
- `bloom_level`: "{% for lvl in bloom_levels.procedural %}{{ lvl }}{{ " | " if not loop.last }}{% endfor %}"
- `child`: **Single ProceduralKnowledge** (the immediate next step) OR `null` (if final step)
- ⚠️ **CRITICAL**: Forms a linear chain - each step has ONE child pointing to the next step
- Contains: common_errors, percent_done (0-100)

**4. AssessmentKnowledge** (Quizzes, evaluation criteria, challenges)
- `type`: "assessment"
- `bloom_level`: "{% for lvl in bloom_levels.assessment %}{{ lvl }}{{ " | " if not loop.last }}{% endfor %}"
- `children`: **None** (leaf nodes)
- Contains: linked_challenges, objectives, question_prompts, evaluation_criteria

## Chain of Thought Process

### For Each Piece of Information:

**Step 1: Classify Knowledge Type**
```
Ask: What is this information?
├─ Definition/Theory/Concept → ConceptualKnowledge
├─ Step/Procedure/How-to → ProceduralKnowledge
└─ Question/Quiz/Evaluation → AssessmentKnowledge
```

**Step 2: Determine Parent**
```
If ConceptualKnowledge:
  → Parent is RootKnowledge (if top-level concept)
  → OR another ConceptualKnowledge (if sub-concept)

If ProceduralKnowledge:
  → Parent is ConceptualKnowledge (goes in its children array)
  → If multi-step: each step's parent is the previous step (via child field)

If AssessmentKnowledge:
  → Parent is ConceptualKnowledge (goes in its children array)
```

**Step 3: Create Children/Connections**
```
If ConceptualKnowledge:
  ├─ Add to children: [ProceduralKnowledge and/or AssessmentKnowledge]
  └─ Add to connections: [Links to other ConceptualKnowledge]

If ProceduralKnowledge:
  └─ Set child: Next ProceduralKnowledge step (or null if final)

If AssessmentKnowledge:
  └─ No children (terminal node)
```

## Conceptual Connections (metadata, not parent-child)

Use these relationship types in the `connections` field:
{% for rel in relation_types -%}
- **{{ rel }}**: {{ relation_descriptions.get(rel, 'Semantic relationship between concepts') }}
{% endfor %}

## Field Requirements

### Required for ALL knowledge types:
- `id`: Unique identifier (UUID format)
- `type`: Knowledge type
- `name`: Internal reference name (snake_case)
- `label`: Human-readable label
- `difficulty`: "{% for d in difficulty_levels %}{{ d }}{{ " | " if not loop.last }}{% endfor %}"
- `source`: "{filename} [page X]" format
- `confidence_score`: 0.0 - 1.0
- `relevance_score`: 0.0 - 1.0
- `validation_status`: "{% for st in validation_statuses %}{{ st }}{{ " | " if not loop.last }}{% endfor %}"
- `visibility`: Set of ["{% for vis in visibility_options %}{{ vis }}{{ '", "' if not loop.last }}{% endfor %}"]

### Type-Specific Fields:

**ConceptualKnowledge:**
- `definition`: Clear explanation
- `prerequisites`: List of prerequisite concept names
- `misconceptions`: Common misunderstandings
- `learning_objective`: What students should learn
- `connections`: Array of {relation, to} objects
- `children`: Array of ProceduralKnowledge | AssessmentKnowledge

**ProceduralKnowledge:**
- `common_errors`: List of pitfalls
- `learning_objective`: What students should do
- `percent_done`: 0-100 (incremental for multi-step procedures)
- `child`: Single next step OR null
- `code_snippet`: **Exact code from the document for this step** (copy verbatim — do NOT paraphrase or omit)
- `hint`: A guiding hint for students that explains *why* this step matters or what to watch out for

**AssessmentKnowledge:**
- `linked_challenges`: Set of challenge identifiers
- `objectives`: List of assessment goals
- `question_prompts`: List of questions
- `evaluation_criteria`: List of criteria

## Node Evolution & Updates

**CRITICAL PRINCIPLE**: Nodes are **living entities** that evolve as more information is discovered across pages.

### When to UPDATE an Existing Node:
✅ **Same concept, more detail** - New page provides additional information about an existing concept
✅ **Clarification** - Later pages clarify or expand on earlier mentions
✅ **Additional steps** - More procedural steps for an existing procedure
✅ **New examples** - Additional misconceptions, common errors, or examples
✅ **Better definition** - More complete or accurate definition found
✅ **Relationship discovery** - New prerequisites or connections identified
✅ **Assessment expansion** - More questions or criteria for existing assessment

### How to Update Nodes:

**For ConceptualKnowledge:**
```
Existing: {
  "definition": "Basic definition...",
  "prerequisites": ["concept_a"],
  "misconceptions": ["misconception_1"]
}

Page 5 adds: More detailed definition + new prerequisite + another misconception

Updated: {
  "definition": "Enhanced, more complete definition...",
  "prerequisites": ["concept_a", "concept_b"],  // Append
  "misconceptions": ["misconception_1", "misconception_2"],  // Append
  "source": "file.pdf [page 1, 5]"  // Track all pages
}
```

**For ProceduralKnowledge:**
```
Existing: Step1 → Step2 → null (percent_done: 50)

Page 3 reveals: Step 3 and Step 4

Updated: Step1 → Step2 → Step3 → Step4 → null
         (Update Step2.child = Step3)
         (Create Step3, Step4)
         (Update percent_done across chain)
```

**For AssessmentKnowledge:**
```
Existing: {
  "question_prompts": ["Q1", "Q2"],
  "evaluation_criteria": ["C1"]
}

Page 7 adds: More questions and criteria

Updated: {
  "question_prompts": ["Q1", "Q2", "Q3", "Q4"],  // Append
  "evaluation_criteria": ["C1", "C2", "C3"]  // Append
}
```

### Update Decision Tree:
```
New information found → Does node with this name exist?
                        ├─ YES → Is it the SAME concept?
                        │        ├─ YES → UPDATE existing node
                        │        └─ NO → CREATE new node (different concept, similar name)
                        └─ NO → CREATE new node
```

### Metadata Updates:

When updating a node:
- **source**: Append new page numbers → `"file.pdf [page 1, 3, 5]"`
- **confidence_score**: Increase if new evidence confirms, decrease if contradicts
- **relevance_score**: Adjust based on cumulative importance
- **validation_status**: May change from "pending" to "verified" with more evidence
- **percent_done**: Recalculate for procedural chains
- **Arrays**: APPEND, don't replace (prerequisites, misconceptions, etc.)
- **Scalars**: REPLACE with better/more complete version (definition, learning_objective)

## Critical Rules

### ✅ DO:
1. **Update existing nodes**: If a concept with the same `name` exists, UPDATE it with new information (don't duplicate)
2. **Track evolution**: Record all page numbers where concept appears in source field
3. **Append to arrays**: Add to prerequisites, misconceptions, common_errors, etc.
4. **Refine definitions**: Replace with more complete/accurate versions when found
5. **Extend procedural chains**: Add new steps to existing procedures
6. **Use consistent IDs**: Keep the same ID for a node across all pages
7. **Set appropriate Bloom levels** based on cognitive complexity
8. **Assign confidence scores** that reflect cumulative evidence

### ❌ DON'T:
1. **Duplicate nodes**: Never create a new node if one with the same name exists
2. **Replace arrays**: Don't overwrite arrays - append new items instead
3. **Create circular references**: Node A → Node B → Node A
4. **Mix parent-child with connections**: Use connections for ConceptualKnowledge relationships only
5. **Create orphan nodes**: Every node (except Root) must have a parent
6. **Skip validation fields**: All required fields must be present
7. **Ignore contradictions**: If new info contradicts old, adjust confidence_score and note in misconceptions

## Automatic Coverage

For any document:
1. **Identify all sections**: Challenges, exercises, modules, topics
2. **Extract concepts**: Main ideas, definitions, theories
3. **Capture procedures**: Step-by-step instructions, algorithms
4. **Note assessments**: Questions, evaluation criteria, quizzes
5. **Ensure completeness**: Every section should be represented in the graph

## Output Format

Return a single JSON object with this structure:
```json
{
  "id": "uuid-string",
  "type": "root",
  "name": "Central node",
  "sources": ["{filename}"],
  "children": [/* ConceptualKnowledge nodes */]
}
```

**Example structure:**
```json
{{ example_json }}
```

## Hinting & Pedagogy

When creating hints or explanations:
- **General hints**: High-level direction (e.g., "Consider the initialization parameters")
- **Specific hints**: Point to relevant concepts (e.g., "Review the Project class constructor")
- **Structural hints**: Provide scaffolding (e.g., "First load the binary, then create the state")

## Code Extraction — CRITICAL

For **ProceduralKnowledge** nodes, you MUST extract code with maximum fidelity:
- **`code_snippet`**: Copy the exact code from the document verbatim. Do NOT paraphrase, summarize, or omit any lines. If the document shows a complete function or block, include the full block.
- **`hint`**: Write a pedagogical hint explaining *why* this code works this way, what parameters mean, or what to watch out for. This should help a student understand the step without just copying blindly.
- If a step has no code in the document, leave `code_snippet` empty — do not invent code.
- Be maximally detailed: a student should be able to implement the solution from your extracted steps alone.

---

You will receive page-by-page content to process incrementally. Follow the Chain of Thought process for each page and merge results into the cumulative knowledge graph.
````

</details>

### Document extraction: user prompt

**Source:** `app/templates/prompts/knowledge_user_prompt.j2`, rendered in `app/services/knowledge.py:382` · **Agent:** `knowledge_file_agent`. Read [the template bugs](#extraction-prompt-bugs) alongside this: what the model receives differs from what the template intends.

<details>
<summary>Full template</summary>

````jinja
## Processing Context

**Document:** {{ source_filename }}
**Total Pages:** {{ total_pages }}
**Processing Mode:** Incremental page-by-page construction

{% for page in page_list %}
---

## Page {{ page.num }} - Extracted Content

### Textual Content:
```
{{ page.text }}
```

### Visual Content (OCR/Diagrams):
```
{{ page.visual }}
```

---
{% endfor %}

---

## Cumulative Knowledge Graph (from previous pages):

```json
{{ root_knowledge | tojson(indent=2) }}
```

---

## Processing Instructions for each Page

### Step 1: Analyze Each Page
Apply Chain of Thought reasoning to the extracted content:

1. **Identify knowledge items**: What concepts, procedures, or assessments are on this page?
2. **Classify each item**: ConceptualKnowledge? ProceduralKnowledge? AssessmentKnowledge?
3. **Check for continuations**: Does content continue from other pages?
4. **Find relationships**: How does this page's content relate to existing nodes?

### Step 2: Merge or Create Nodes

**CRITICAL: Nodes evolve as you process more pages. Always UPDATE existing nodes with new information rather than creating duplicates.**

**BEFORE creating any new node, ask:**
- ✓ Does a node with this `name` already exist in the graph?
  - **YES** → UPDATE the existing node with new information
  - **NO** → CREATE a new node

#### When to UPDATE Existing Nodes:

**Scenario 1: Additional Detail**
```
Page 2: "angr is a binary analysis framework"
Page 5: "angr uses symbolic execution to explore program paths and uses Z3 solver"

Action: UPDATE "angr_framework" node
- Enhance definition with solver details
- Add "Z3 solver" to related concepts
- Update source: "file.pdf [page 2, 5]"
```

**Scenario 2: New Prerequisites Discovered**
```
Page 3: Introduces "symbolic_execution" concept
Page 7: Mentions "symbolic_execution requires understanding of constraint solving"

Action: UPDATE "symbolic_execution" node
- Add "constraint_solving" to prerequisites array
- Don't replace existing prerequisites
```

**Scenario 3: Procedural Step Continuation**
```
Page 4: Steps 1-2 of "initialize_angr_project"
Page 6: Steps 3-4 of the same procedure

Action: UPDATE procedural chain
- Extend Step2.child to point to Step3
- Create Step3 and Step4
- Update percent_done across all steps
```

**Scenario 4: Assessment Expansion**
```
Page 8: Quiz has questions Q1, Q2
Page 12: Reveals Q3, Q4 and additional evaluation criteria

Action: UPDATE "angr_basics_quiz" node
- Append Q3, Q4 to question_prompts
- Append new criteria to evaluation_criteria
- Increase relevance_score if assessment is more comprehensive
```

**Scenario 5: Clarification/Correction**
```
Page 2: Vague definition "angr helps with binaries"
Page 10: Detailed explanation with proper terminology

Action: UPDATE "angr_framework" node
- REPLACE definition with more accurate version
- INCREASE confidence_score (more evidence)
- Keep both page numbers in source
```

#### When to CREATE New Nodes:

**Scenario 1: Genuinely New Concept**
```
Page 3: First mention of "constraint_solving"

Action: CREATE new ConceptualKnowledge node
- Generate new UUID
- Set source to "file.pdf [page 3]"
- Link to appropriate parent
```

**Scenario 2: Different Concept, Similar Name**
```
Existing: "symbolic_state" (angr-specific concept)
Page 8: "symbolic_state" (general program analysis concept)

Action: CREATE new node with disambiguated name
- Name: "symbolic_state_general" vs "symbolic_state_angr"
- Or update existing if it's actually the same concept
```

#### Update Rules by Node Type:

**ConceptualKnowledge Updates:**
```python
# Fields to APPEND (arrays):
- prerequisites: Add newly discovered dependencies
- misconceptions: Add new misunderstandings found
- connections: Add new relationships to other concepts

# Fields to REPLACE (when better info found):
- definition: More complete/accurate definition
- learning_objective: More precise objective
- difficulty: Adjust if content reveals different complexity

# Fields to RECALCULATE:
- confidence_score: Based on cumulative evidence
- relevance_score: Based on importance in full context
- source: Append all page numbers where mentioned
```

**ProceduralKnowledge Updates:**
```python
# Extend the chain:
- child: Point to newly discovered next step
- percent_done: Recalculate based on total steps

# Fields to APPEND:
- common_errors: Add new pitfalls discovered

# Fields to REPLACE:
- learning_objective: More complete description
- code_snippet: Replace with more complete/accurate code if found on later pages
- hint: Replace with more detailed hint if found on later pages
```

**AssessmentKnowledge Updates:**
```python
# Fields to APPEND:
- linked_challenges: Add newly mentioned challenges
- objectives: Add new assessment goals
- question_prompts: Add new questions
- evaluation_criteria: Add new criteria

# DO NOT create separate assessment nodes for the same topic
```

#### Examples of WRONG Behavior:

❌ **Creating duplicates:**
```json
// Page 2 creates:
{"name": "angr_framework", "definition": "Brief definition"}

// Page 5 WRONGLY creates:
{"name": "angr_framework_advanced", "definition": "Detailed definition"}

// CORRECT: Update the existing node instead
```

❌ **Replacing arrays:**
```json
// Existing:
{"misconceptions": ["misconception_1", "misconception_2"]}

// Page 7 WRONGLY replaces:
{"misconceptions": ["misconception_3"]}  // Lost 1 & 2!

// CORRECT:
{"misconceptions": ["misconception_1", "misconception_2", "misconception_3"]}
```

❌ **Breaking procedural chains:**
```json
// Existing: Step1 → Step2 → null
// Page 5 WRONGLY creates parallel chain: StepA → StepB → null

// CORRECT: Extend existing chain: Step1 → Step2 → Step3 → Step4 → null
```

### When updating existing nodes:
**Summary - Update Decision Process:**
```
For each piece of information on this page:

1. Identify what it describes (concept/procedure/assessment)
2. Search existing graph for matching name
3. If found:
   ✓ Same concept → UPDATE with new information
   ✓ Different concept → CREATE new node with unique name
4. If not found:
   ✓ CREATE new node
5. Track source page numbers for all updates
6. Adjust confidence/relevance scores based on cumulative evidence
```

**When creating new nodes:**
- Generate a unique `id` (UUID format)
- Set `source` to "{{ source_filename }} [page {{ page_num }}]"
- Follow hierarchy rules (see system prompt)
- Link to appropriate parent node
- Add connections to related ConceptualKnowledge

### Step 3: Handle Procedural Chains

If this page contains procedural steps:
```
Existing: P01-step1 → P01-step2 → null
This page: Continues with step 3, step 4

Result: P01-step1 → P01-step2 → P01-step3 → P01-step4 → null
         (update P01-step2.child = P01-step3)
         (create P01-step3, P01-step4)
```

### Step 4: Update Metadata

For all nodes touched on this page:
- Update `source` to include "{{ source_filename }} [page {{ page_num }}]"
- Adjust `percent_done` for procedural nodes based on completion
- Set `validation_status` to "pending" for newly extracted content
- Assign appropriate `confidence_score` and `relevance_score`

### Step 5: Validate Structure & Updates

Before returning, verify:

**Structural Validation:**
- ✓ RootKnowledge has only ConceptualKnowledge children
- ✓ ConceptualKnowledge has only ProceduralKnowledge/AssessmentKnowledge children
- ✓ ProceduralKnowledge chains are linear (single child per node)
- ✓ AssessmentKnowledge has no children
- ✓ No circular references exist
- ✓ All required fields are populated

**Update Validation:**
- ✓ No duplicate nodes with the same `name` exist
- ✓ All nodes touched on this page have updated `source` with the page number
- ✓ Array fields were APPENDED to, not replaced
- ✓ Procedural chains are properly extended (no gaps)
- ✓ Confidence scores reflect cumulative evidence
- ✓ IDs are consistent with previous pages

**Data Quality Checks:**
- ✓ Definitions are clear and complete (updated if better version found)
- ✓ Prerequisites are comprehensive (all dependencies listed)
- ✓ Procedural percent_done values add up correctly
- ✓ Assessment criteria cover all objectives
- ✓ Source citations include all relevant pages

---

## Output Requirements

Return the **COMPLETE, UPDATED knowledge graph** as a single JSON object.

**CRITICAL:**
- Include ALL nodes (both from previous pages AND this page)
- Do NOT return only the changes - return the full merged graph
- Ensure the structure follows the RootKnowledge schema
- All IDs must remain consistent across pages
- The JSON must be valid and parsable

**Response format:**
```json
{
  "id": "existing-root-id",
  "type": "root",
  "name": "Central node",
  "sources": ["{{ source_filename }}"],
  "children": [/* Complete merged children array */]
}
```

{% if last_page  %}
---
## Final Page Notice

This is the **LAST PAGE** of the document. Ensure:
- All procedural chains are properly terminated (final step.child = null)
- All `percent_done` values are accurate
- All sections/challenges from the document are represented
- The knowledge graph is complete and comprehensive
{% endif %}
````

</details>

---

## Frontend

There is no separate frontend app or build step. Flask renders Jinja2 templates from `app/templates/web/`. Dynamic behaviour comes from HTMX and inline JavaScript, and libraries load from CDNs in `base.html`, so the browser needs internet access.

| File (under `app/templates/web/`) | Purpose |
|---|---|
| `base.html` | Root layout, CDN scripts, markdown rendering and its styles |
| `base_app.html` | Logged-in app shell |
| `auth/login.html`, `auth/register.html` | Sign-in and registration |
| `course/dashboard.html` | Course list and course creation |
| `course/chat.html` | **Main student chat page**, with challenge breadcrumb and survey and hint modals |
| `course/chat_message.html` | **HTML fragment for each new exchange** |
| `course/pre_study.html`, `course/post_study.html` | Pre-study and post-study pages |
| `course/approved_hints.html`, `hint_badge.html` | Student hint modal content and badge |
| `course/hint_item.html` | Instructor's pending-hint list |
| `course/settings.html` | **Instructor settings:** graph, uploads, members, prompts and domain placeholders, hints |
| `course/dashboard_progress.html` | Instructor progress dashboard |
| `course/_struggle_detail.html`, `_student_chat.html` | Dashboard detail and transcript fragments |
| `knowledge/upload_list.html` | Upload status list |
| `survey/form.html`, `form_embed.html`, `list.html`, `success.html` | Survey pages, chat-modal version and result page |

### Student progression

`app/challenges.py` defines 20 stages: `pre_study` (0), `ctf_00`–`ctf_17` (1–18) and `post_study` (19). Submitting a stage's survey unlocks the next one, and earlier challenge chats become read-only.

Survey submission builds a PDF and emails it through Gmail SMTP. **The stage only advances if the email succeeds**, so `SMTP_USER`, `SMTP_PASSWORD` and `SMTP_TO` must be set.

---

## Data model

All data is stored in Neo4j, apart from upload status records, which are kept in memory and lost on restart.

| Node | Purpose | Defined in |
|---|---|---|
| `root` | A course | `app/models/knowledge.py` |
| `conceptual`, `procedural`, `assessment` | Knowledge graph content | `app/models/knowledge.py` |
| `ContentChunk` | Embedded text used for answer retrieval | `app/models/knowledge.py` |
| `User` | Student or instructor | `app/models/user.py` |
| `UserTrajectory` | One record per logged question: retrieved items, scores, repeat count, hint | `app/models/user.py` |
| `SurveyCompletion` | Records a completed survey; drives progression | `app/models/user.py` |
| `ChatMessage` | One chat message, tagged with its challenge stage | `app/models/chat.py` |

| Relationship | Meaning |
|---|---|
| `HAS_CHILD` | Graph hierarchy |
| `HAS_CONTENT` | Course → chunk |
| `TEACHES`, `ENROLLED_IN` | User → course |
| `HAS_TRAJECTORY`, `PREVIOUS_TRAJECTORY` | User → question record; question record → previous one |
| `HAS_CHAT_MESSAGE` | User → message and course → message |
| `COMPLETED_SURVEY` | User → survey completion |

Vector indexes are configured in `config.yaml`: `index_content_chunk_vector` on `ContentChunk.chunk_vector` and `index_trajectory_query_vector` on `UserTrajectory.trajectory_vector`.

---

## Routes

### Student and shared

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/auth/login`, `/auth/register` | Sign in and register |
| POST | `/auth/logout` | Sign out |
| GET | `/` | Course dashboard |
| GET | `/course/<id>/chat` | Redirects students to their current stage; opens chat for instructors |
| POST | `/course/<id>/chat/send` | Sends a message with no challenge stage |
| GET | `/course/<id>/pre-study`, `/post-study` | Study pages |
| GET | `/course/<id>/challenge/<stage>` | Challenge chat |
| POST | `/course/<id>/challenge/<stage>/send` | **Sends a student message** |
| GET | `/course/<id>/hints/student`, `/hints/student/count` | Hint modal and badge |
| POST | `/course/<id>/hints/<trajectory_id>/read` | Marks a hint as read |
| GET | `/survey/list`, `/survey/<id>`, `/survey/<id>/embed` | Survey pages |
| POST | `/survey/<id>/submit` | Submits a survey |

### Instructor

| Method | Path | Purpose |
|---|---|---|
| POST | `/course/create` | Creates a course |
| GET | `/course/<id>/settings` | Settings page |
| POST | `/course/<id>/upload` | **Uploads documents and builds the graph** |
| GET, PUT | `/course/<id>/members` | Reads or updates members |
| DELETE | `/course/<id>/delete`, `/clear` | Deletes or clears a course |
| GET | `/course/<id>/hints` | Pending hints |
| POST | `/course/<id>/hints/manual` | Creates a manual hint |
| PUT | `/course/<id>/hints/<trajectory_id>` | Approves or rejects a hint |
| GET | `/course/<id>/progress` | Progress dashboard |
| GET | `/course/<id>/api/student-chat/<student_id>` | Transcript for one stage (`?stage=`) |
| GET | `/course/<id>/api/student-chat/<student_id>/pdf` | Transcript PDF |
| GET | `/course/<id>/api/node-struggle`, `/api/student-struggle` | Dashboard data |
| GET | `/course/<id>/api/struggle-detail/node/<node_id>`, `/student/<student_id>` | Dashboard detail fragments |

### Knowledge graph (`/knowledge`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/upload/list` | Upload statuses |
| GET | `/graph/data/<knowledge_id>` | Graph as JSON |
| PUT, DELETE | `/node/<node_id>` | Edits or deletes a node |
| POST | `/node/<parent_id>/child` | Adds a child node |
| POST, PUT, DELETE | `/node/<node_id>/relationship` | Manages relationships |

---

## Setup

### Prerequisites

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- Docker, for Neo4j and Ollama
- **Poppler**, for PDF page conversion
- **Tesseract OCR**, for text in images

| Platform | Poppler | Tesseract |
|---|---|---|
| macOS | `brew install poppler` | `brew install tesseract` |
| Debian/Ubuntu | `sudo apt-get install poppler-utils` | `sudo apt-get install tesseract-ocr tesseract-ocr-all` |
| RedHat/CentOS | `sudo yum install poppler-utils` | See the Tesseract documentation |
| Windows | [poppler-windows releases](https://github.com/oschwartz10612/poppler-windows/releases), then add `bin` to `PATH` | `scoop install tesseract tesseract-languages` |

On Windows, if `tesseract-languages` fails with a symbolic link error, enable Developer Mode or run PowerShell as Administrator.

Verify both with `pdftoppm -v` and `tesseract --version`.

### Run the app

```bash
uv sync
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
docker compose up -d        # Neo4j and Ollama
uv run python main.py       # http://127.0.0.1:5000
```

Neo4j Browser is at `http://localhost:7474`.

### Load a course

Register the instructor and student accounts first, since the seed scripts look up existing users.

```bash
uv run python seed_ctf.py -i instructor@uni.edu -s student1@uni.edu student2@uni.edu
uv run python seed_demo.py -i professor@uni.edu -c algorithms
```

`seed_demo.py` creates sample trajectories and hints for testing the UI. Its `-c` option accepts `software-arch` (default) or `algorithms`.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_MODEL` | `ollama:gpt-oss:20b` | Tutor answers, hints and summaries |
| `KNOWLEDGE_FILE_MODEL` | `ollama:gpt-oss:20b` | Document extraction |
| `GRAPH_MODEL` | `ollama:gpt-oss:20b` | GraphRAG wrapper |
| `EMBEDDER_MODEL` | `ollama:qwen3-embedding:8b` | Embeddings |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint |
| `NEO4J_URI` | `neo4j://127.0.0.1:7687` | Neo4j connection |
| `NEO4J_USER`, `NEO4J_PASS` | `neo4j`, `password` | Neo4j login |
| `NEO4J_AUTH` | `neo4j/password` | Neo4j login for Docker Compose |
| `SECRET_KEY` | `dev-secret` | Flask session key; **change it outside local development** |
| `LOG_LEVEL` | `DEBUG` | Log level |
| `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TO` | None | **Required for survey submission;** missing from `.env.example` |
| `ALLOWED_EXTENSIONS` | `pdf,pptx,html` | Currently has no effect; see known issues |
| `FAKE_GENERATION`, `FLASK_DEBUG` | `false`, `true` | Currently have no effect; see known issues |

To use a different provider, set the model variables using `pydantic-ai`'s `provider:model` format, such as `anthropic:claude-sonnet-4-5`, and set that provider's API key.

If you change the embedding model, its vector size may no longer match existing vector indexes. `scripts/drop_trajectory_indexes.py` handles the trajectory index only.

### Development commands

```bash
uv run pytest                                       # all tests
uv run pytest tests/path/to/test_file.py::test_name # one test
uv run ruff check .                                 # lint
uv run ruff format .                                # format
```

### Maintenance scripts

| Script | Purpose |
|---|---|
| `scripts/clear_trajectories.py` | **Deletes all `UserTrajectory` nodes** |
| `scripts/drop_trajectory_indexes.py` | Drops old trajectory indexes so they are recreated |
| `scripts/tag_chunks_with_node_id.py` | Tags chunks with the graph node they came from |

---

## Known issues

These come from reading and, where noted, running the code on 2026-11-02.

### Extraction prompt bugs

`knowledge_user_prompt.j2` and the values passed by `__process_pages_batch` don't match. Rendering the templates confirmed the following:

- **Page numbers are blank.** The template reads `page.num` and `page_num`, but the code passes each page's number as `page["page_num"]`, so the model sees `Page  - Extracted Content` and `[page ]`.
- **Page content is a Python object dump.** `page.text` is a whole content object, so the model receives `page=1 text='…' from_image=False` rather than just the text.
- **Code in documents is HTML-escaped.** The environment has `autoescape=True`, so `<` becomes `&lt;` and `'` becomes `&#39;`. This works against the prompt's own instruction to copy code verbatim. The same setting escapes the system prompt's example JSON (`&#34;id&#34;`).
- **The cumulative graph is double-encoded.** It is already a JSON string before `tojson`, so the model sees one quoted string containing `\n` and `\"`.
- **Some variables are never passed.** `total_pages` and `last_page` are missing, so the total is blank and the "Final Page Notice" never appears. `field_docs` is passed to the system template but never used.

### Extraction behaviour

- **HTML files produce no extraction pages, and some slides get dropped.** Pages are built with `zip(text, visual, numbers)`, which stops at the shorter list. HTML has no OCR entries, so its LLM prompt contains no pages. PPTX OCR entries are per image, so slides can be dropped or paired with the wrong image text.
- **Graph-building may time out on larger batches.** `knowledge_file_agent` has a 30-second limit, yet each call can cover 20 pages and return the entire graph.
- **Slimmed graph context can lose data.** Each batch sees a reduced graph without fields like `code_snippet`, but must return the complete graph, so earlier details can be dropped.
- **Uploaded documents lose concept relationships.** `add_document_to_course` doesn't save `connections`; only `create_knowledge` does.

### Retrieval and tutoring

- **Retrieval isn't limited to the current course.** `retriever.search` is called without a course filter, so one course's material can appear in another's answers.
- **Re-ranking currently acts like a score sort.** `ContentChunk` nodes have no `node_type` or `difficulty`, so those criteria normally tie.
- **There's no second retrieval source.** `content_rag` isn't wired in, so low-confidence results return the fallback message.
- **Some code isn't called in production.** That includes `__scaffold_from_chunks` and `__get_interaction_type`.

### Security and deployment

- **Debug mode is always on.** `main.py` hard-codes `debug=True` and binds to `0.0.0.0`. Don't expose it publicly in this form.
- **Anyone can register as an instructor.** The registration form offers a role choice and the server accepts it.
- **Instructor access isn't limited to a user's own courses.** `roles_required` checks only the role, so any instructor can reach any course's settings, transcripts and delete route.
- **Rendered markdown isn't sanitized.** `marked.parse` output goes straight into `innerHTML`, so HTML in a model response or stored message could run in the viewer's browser.

### Configuration

- **`ALLOWED_EXTENSIONS` has no effect.** `config.yaml` nests it under `core.uploads`, but `app/containers.py` reads `core.allowed_extensions`, which resolves to `[]`.
- **`FAKE_GENERATION` and `FLASK_DEBUG` have no effect.** No application code reads either setting.
- **Docker's Ollama image pulls different models.** `.docker/ollama.entrypoint.sh` pulls `nomic-embed-text` and `llama3`, not the defaults in `config.yaml`, so you must pull the defaults or change your `.env`.
- **Embedding-failure fallbacks hard-code vector sizes.** They use 384 dimensions in `app/services/` and 768 in `seed_ctf.py`, which may not match your embedding model.

### Tests and leftovers

- **The test suite is failing.** `uv run pytest` reports **11 failed, 172 passed**, across `test_course` (controllers and views), `test_neo4j`, `test_user` and `test_dashboard`.
- **`templates/dashboard.html` at the repository root is unused.** The live page is `app/templates/web/course/dashboard.html`.
