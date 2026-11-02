"""Seed the CTF knowledge graph from chunks.json.

Does two things:
  1. Maps chunks.json into app models and persists the full KG to Neo4j
     via create_knowledge() (gives the UI the correct course structure).
  2. Extracts rich text from every node (definitions, hints, code snippets,
     objectives) and stores them as ContentChunk nodes with embeddings so
     the supervisor agent can search and answer student questions.

Usage
-----
    uv run python seed_ctf.py -i <instructor_email> -s <student_email> [...]
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app.containers import Application
from app import models


# ---------------------------------------------------------------------------
# Difficulty / Bloom mappers
# ---------------------------------------------------------------------------

def _difficulty(raw: str) -> models.KnowledgeDifficulty:
    mapping = {
        "beginner": models.KnowledgeDifficulty.EASY,
        "easy":     models.KnowledgeDifficulty.EASY,
        "intermediate": models.KnowledgeDifficulty.MEDIUM,
        "medium":   models.KnowledgeDifficulty.MEDIUM,
        "advanced": models.KnowledgeDifficulty.HARD,
        "hard":     models.KnowledgeDifficulty.HARD,
    }
    return mapping.get(raw.lower(), models.KnowledgeDifficulty.MEDIUM)


def _bloom_conceptual(raw: str) -> models.KnowledgeConceptualBloomLevel:
    mapping = {
        "remember":   models.KnowledgeConceptualBloomLevel.REMEMBER,
        "understand": models.KnowledgeConceptualBloomLevel.UNDERSTAND,
        # Analyze/Apply/etc. don't exist for conceptual — fall back to understand
    }
    return mapping.get(raw.lower(), models.KnowledgeConceptualBloomLevel.UNDERSTAND)


def _bloom_procedural(raw: str) -> models.KnowledgeProceduralBloomLevel:
    mapping = {
        "apply":   models.KnowledgeProceduralBloomLevel.APPLY,
        "analyze": models.KnowledgeProceduralBloomLevel.ANALYZE,
    }
    return mapping.get(raw.lower(), models.KnowledgeProceduralBloomLevel.APPLY)


def _bloom_assessment(raw: str) -> models.KnowledgeAssessmentBloomLevel:
    mapping = {
        "evaluate": models.KnowledgeAssessmentBloomLevel.EVALUATE,
        "create":   models.KnowledgeAssessmentBloomLevel.CREATE,
    }
    return mapping.get(raw.lower(), models.KnowledgeAssessmentBloomLevel.EVALUATE)


# ---------------------------------------------------------------------------
# JSON → model builders
# ---------------------------------------------------------------------------

def _build_proc_step(node: dict) -> models.ProceduralKnowledge:
    """Recursively build a linked-list of procedural steps."""
    raw_children = node.get("children", [])
    child = _build_proc_step(raw_children[0]) if raw_children else None

    hint = node.get("hint", "")
    code = node.get("code_snippet", "")
    learning_obj = f"{hint} Code: {code}".strip(" .") if (hint or code) else node["name"]

    return models.ProceduralKnowledge(
        name=node["id"],
        label=node["name"],
        difficulty=_difficulty(node.get("difficulty", "medium")),
        bloom_level=_bloom_procedural(node.get("bloom_level", "apply")),
        common_errors=node.get("common_errors", []),
        visibility=[models.KnowledgeVisibility.SUPERVISOR_AGENT],
        validation_status=models.KnowledgeValidationStatus.PENDING,
        confidence_score=node.get("confidence", 0.85),
        relevance_score=node.get("relevance_score", 0.85),
        source=node.get("source", "CTF_updated.pdf"),
        learning_objective=learning_obj,
        percent_done=0.0,
        child=child,
    )


def _build_procedural_parent(node: dict) -> models.ProceduralKnowledge:
    """Build the top-level procedural node whose child is the first step."""
    steps = node.get("children", [])
    first_step = _build_proc_step(steps[0]) if steps else None

    description = node.get("description", node["name"])
    return models.ProceduralKnowledge(
        name=node["id"],
        label=node["name"],
        difficulty=_difficulty(node.get("difficulty", "medium")),
        bloom_level=_bloom_procedural(node.get("bloom_level", "apply")),
        common_errors=node.get("common_errors", []),
        visibility=[models.KnowledgeVisibility.SUPERVISOR_AGENT],
        validation_status=models.KnowledgeValidationStatus.PENDING,
        confidence_score=node.get("confidence", 0.85),
        relevance_score=node.get("relevance_score", 0.85),
        source=node.get("source", "CTF_updated.pdf"),
        learning_objective=description,
        percent_done=0.0,
        child=first_step,
    )


def _build_assessment(node: dict) -> models.AssessmentKnowledge:
    question_prompts = [
        q["question"] if isinstance(q, dict) else str(q)
        for q in node.get("question_prompts", [])
    ]
    return models.AssessmentKnowledge(
        name=node["id"],
        label=node["name"],
        difficulty=_difficulty(node.get("difficulty", "medium")),
        bloom_level=_bloom_assessment(node.get("bloom_level", "evaluate")),
        linked_challenges=node.get("linked_challenges", []),
        objectives=node.get("objectives", []),
        question_prompts=question_prompts,
        evaluation_criteria=node.get("evaluation_criteria", []),
    )


def _build_concept(node: dict) -> models.ConceptualKnowledge:
    children = []
    for child in node.get("children", []):
        cid = child.get("id", "")
        if cid.startswith("P"):
            children.append(_build_procedural_parent(child))
        elif cid.startswith("A"):
            children.append(_build_assessment(child))

    return models.ConceptualKnowledge(
        name=node["id"],
        label=node["name"],
        difficulty=_difficulty(node.get("difficulty", "medium")),
        bloom_level=_bloom_conceptual(node.get("bloom_level", "understand")),
        definition=node.get("definition", ""),
        prerequisites=node.get("prerequisites", []),
        misconceptions=node.get("misconceptions", []),
        visibility=[models.KnowledgeVisibility.SUPERVISOR_AGENT],
        validation_status=models.KnowledgeValidationStatus.PENDING,
        confidence_score=node.get("confidence", 0.85),
        relevance_score=node.get("relevance_score", 0.85),
        source=node.get("source", "CTF_updated.pdf"),
        learning_objective=node.get("learning_objective", ""),
        connections=[],  # wired after all concepts are built
        children=children,
    )


def _build_root(data: dict) -> tuple[models.RootKnowledge, dict[str, str]]:
    """
    Build the full RootKnowledge tree.
    Returns (root, json_id_to_model_id) so connections can be wired.
    """
    concept_nodes = [_build_concept(c) for c in data.get("children", [])]

    # Map JSON id (e.g. "C01") → model UUID for wiring connections
    json_children = data.get("children", [])
    id_map: dict[str, str] = {
        jc["id"]: mc.id
        for jc, mc in zip(json_children, concept_nodes)
    }

    # Wire connections now that all UUIDs are known
    for jc, mc in zip(json_children, concept_nodes):
        mc.connections = [
            models.ConceptualKnowledgeConnection(
                relation=models.KnowledgeConceptualLinkType(conn["relation"]),
                to=id_map[conn["to"]],
            )
            for conn in jc.get("connections", [])
            if conn["to"] in id_map
        ]

    root = models.RootKnowledge(
        name=data.get("name", "CTF Knowledge Graph"),
        description="Angr symbolic execution CTF course generated from CTF_updated.pdf",
        sources=["CTF_updated.pdf"],
        children=concept_nodes,
    )
    return root, id_map


# ---------------------------------------------------------------------------
# Text extraction for ContentChunk nodes
# ---------------------------------------------------------------------------

def _chunks_from_node(node: dict, course_id: str, chunk_index_start: int) -> list[models.ContentChunk]:
    chunks = []
    idx = chunk_index_start
    source = "CTF_updated.pdf"

    def page_from_source(src: str) -> int:
        """Best-effort page extraction from source strings like '[pages 2–7]'."""
        import re
        m = re.search(r"(\d+)", src or "")
        return int(m.group(1)) if m else 0

    def add(text: str, page: int = 0):
        nonlocal idx
        text = text.strip()
        if text:
            chunks.append(models.ContentChunk(
                content=text,
                page=page,
                source_file=source,
                chunk_index=idx,
                course_id=course_id,
                chunk_type=models.ContentChunkType.PARAGRAPH,
            ))
            idx += 1

    node_id = node.get("id", "")

    if node_id.startswith("C"):
        page = page_from_source(node.get("source", ""))
        name = node.get("name", "")
        definition = node.get("definition", "")
        obj = node.get("learning_objective", "")
        misconceptions = node.get("misconceptions", [])

        add(f"{name}: {definition}", page)
        if obj:
            add(f"Learning objective for {name}: {obj}", page)
        if misconceptions:
            add(f"Common misconceptions about {name}: {'; '.join(misconceptions)}", page)

        for child in node.get("children", []):
            child_chunks = _chunks_from_node(child, course_id, idx)
            chunks.extend(child_chunks)
            idx += len(child_chunks)

    elif node_id.startswith("P") and "-step" not in node_id:
        # Procedural parent
        desc = node.get("description", node.get("name", ""))
        errors = node.get("common_errors", [])
        add(f"{node.get('name', '')}: {desc}")
        if errors:
            add(f"Common errors in {node.get('name', '')}: {'; '.join(errors)}")

        for child in node.get("children", []):
            child_chunks = _chunks_from_node(child, course_id, idx)
            chunks.extend(child_chunks)
            idx += len(child_chunks)

    elif "-step" in node_id:
        # Procedural step
        step_name = node.get("name", node_id)
        hint = node.get("hint", "")
        code = node.get("code_snippet", "")
        parts = [step_name]
        if hint:
            parts.append(f"Hint: {hint}")
        if code:
            parts.append(f"Code: {code}")
        add(" — ".join(parts))

        for child in node.get("children", []):
            child_chunks = _chunks_from_node(child, course_id, idx)
            chunks.extend(child_chunks)
            idx += len(child_chunks)

    elif node_id.startswith("A"):
        name = node.get("name", "")
        objectives = node.get("objectives", [])
        questions = [
            q["question"] if isinstance(q, dict) else str(q)
            for q in node.get("question_prompts", [])
        ]
        criteria = node.get("evaluation_criteria", [])

        if objectives:
            add(f"{name} — Objectives: {'; '.join(objectives)}")
        if questions:
            add(f"{name} — Key questions: {'; '.join(questions)}")
        if criteria:
            add(f"{name} — Evaluation criteria: {'; '.join(criteria)}")

    return chunks


def _extract_all_chunks(data: dict, course_id: str) -> list[models.ContentChunk]:
    all_chunks = []
    idx = 0
    for concept in data.get("children", []):
        new_chunks = _chunks_from_node(concept, course_id, idx)
        all_chunks.extend(new_chunks)
        idx += len(new_chunks)
    return all_chunks


# ---------------------------------------------------------------------------
# ContentChunk persistence (replicates KnowledgeService.__store_content_chunks)
# ---------------------------------------------------------------------------

def _store_chunks(chunks: list[models.ContentChunk], container, course_id: str) -> None:
    if not chunks:
        return

    embedder = container.services.knowledge().__dict__.get("_KnowledgeService__embedder")

    # Access the embedder and session factory via the container
    raw_embedder = container.gateways.neo4j_embedder()
    driver = container.gateways.neo4j_driver()

    BATCH = 32
    with driver.session() as session:
        for i in range(0, len(chunks), BATCH):
            batch = chunks[i: i + BATCH]
            texts = [c.content for c in batch]
            try:
                vectors = [raw_embedder.embed_query(t) for t in texts]
            except Exception as e:
                print(f"  [warn] Embedding failed for batch {i//BATCH}: {e} — using zero vectors")
                vectors = [[0.0] * 768 for _ in texts]

            tx = session.begin_transaction()
            try:
                for chunk, vector in zip(batch, vectors):
                    props = chunk.model_dump()
                    props["chunk_vector"] = vector
                    tx.run(
                        "MATCH (root {id: $course_id}) "
                        "CREATE (c:ContentChunk $props) "
                        "CREATE (root)-[:HAS_CONTENT]->(c)",
                        course_id=course_id,
                        props=props,
                    )
                tx.commit()
                print(f"  stored chunks {i+1}–{min(i+BATCH, len(chunks))} of {len(chunks)}")
            except Exception as e:
                tx.rollback()
                raise


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def _require_user(user_service, email: str) -> models.User:
    user = user_service.get_user_by_email(email)
    if not user:
        print(f"[error] No user found with email '{email}'. Register them first.")
        sys.exit(1)
    return user


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the CTF knowledge graph from chunks.json."
    )
    parser.add_argument("-i", "--instructor", required=True, metavar="EMAIL")
    parser.add_argument("-s", "--students", nargs="+", required=True, metavar="EMAIL")
    parser.add_argument(
        "--chunks", default="chunks.json", metavar="FILE",
        help="Path to the JSON knowledge graph file (default: chunks.json)"
    )
    args = parser.parse_args()

    chunks_path = Path(args.chunks)
    if not chunks_path.exists():
        print(f"[error] File not found: {chunks_path}")
        sys.exit(1)

    data = json.loads(chunks_path.read_text())

    container = Application()
    container.init_resources()

    user_service = container.services.user()
    knowledge_service = container.services.knowledge()

    instructor = _require_user(user_service, args.instructor)
    students = [_require_user(user_service, e) for e in args.students]

    print(f"\nInstructor : {instructor.name} <{instructor.email}>")
    print(f"Students   : {', '.join(s.email for s in students)}")

    # 1. Build and persist the knowledge graph
    print("\nBuilding knowledge graph from chunks.json …")
    root, _ = _build_root(data)
    course_id = knowledge_service.create_knowledge(root)
    print(f"  course id : {course_id}")
    print(f"  concepts  : {len(root.children)}")

    # 2. Assign members
    knowledge_service.set_course_instructors(course_id, [instructor.id])
    knowledge_service.set_course_students(course_id, [s.id for s in students])
    print(f"  members   : instructor + {len(students)} student(s) assigned")

    # 3. Extract and store ContentChunk nodes with embeddings
    print("\nExtracting content chunks …")
    chunks = _extract_all_chunks(data, course_id)
    print(f"  {len(chunks)} chunks extracted — embedding and storing …")
    _store_chunks(chunks, container, course_id)

    print(f"\nDone. CTF course ready (id={course_id}).\n")


if __name__ == "__main__":
    main()
