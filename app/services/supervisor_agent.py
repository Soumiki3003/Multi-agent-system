import logging
import re
import time
from pydantic import BaseModel

from neo4j_graphrag.generation import GraphRAG
from neo4j_graphrag.types import LLMMessage
from pydantic_ai import Agent

from app import models
from enum import StrEnum

from .ctf_reference import CTFReferenceService
from .user import UserService


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Signals that a message is really a graded survey/knowledge-test/quiz question
# (see app/surveys/data.py) rather than a genuine question about the challenge —
# explicit mentions of the instruments themselves, plus the Likert/AI-feedback
# phrasing used verbatim in their question labels.
_SURVEY_KEYWORDS = [
    "survey",
    "questionnaire",
    "knowledge test",
    "pre-study",
    "post-study",
    "post-challenge",
    "quiz",
    "likert",
    "strongly agree",
    "strongly disagree",
    "select the single best answer",
    "select all that apply",
    "rate each statement",
    "learning outcome",
    "participant id",
    "was the ai assistant",
    "did the ai assistant provide",
    "incorrect or misleading information",
]
# A copy-pasted multiple-choice question (as used throughout data.py) looks like
# several "A) ...", "B. ...", "C) ..." option lines in a row.
_MC_OPTION_LINE_RE = re.compile(r"(?m)^\s*[a-dA-D][\).]\s+\S")


class SupervisorResult(BaseModel):
    answer: str
    hint_text: str | None = None
    hint_reason: str | None = None


class Intent(StrEnum):
    DEFINITION = "definition"
    PROCEDURAL = "procedural"
    TROUBLESHOOTING = "troubleshooting"
    EXAMPLE_REQUEST = "example_request"
    CONTEXT_REQUEST = "context_request"


class SupervisorAgentService:
    RESPONSE_FALLBACK = "I couldn't find relevant information to answer your question. Please try rephrasing."
    SURVEY_DECLINE_RESPONSE = (
        "That looks like a survey, quiz, or knowledge-test question rather than a "
        "question about your current challenge — I can't help answer those, since "
        "they're meant to reflect your own understanding. Happy to help if you have "
        "a genuine question about the challenge itself!"
    )

    INTENT_KEYWORDS: dict[Intent, list[str]] = {
        Intent.DEFINITION: [
            "what is",
            "what are",
            "explain",
            "define",
            "definition",
            "meaning of",
            "describe",
        ],
        Intent.PROCEDURAL: [
            "how to",
            "how do",
            "steps to",
            "implement",
            "procedure",
            "how can i",
            "what steps",
        ],
        Intent.TROUBLESHOOTING: [
            "error in",
            "not working",
            "fix",
            "bug",
            "issue",
            "broken",
            "fails",
            "exception",
            "debug",
        ],
        Intent.EXAMPLE_REQUEST: [
            "example of",
            "show me",
            "example",
            "sample",
            "demo",
            "give me an example",
        ],
    }

    INTENT_TO_PREFERRED_TYPES: dict[Intent, list[models.KnowledgeType]] = {
        Intent.DEFINITION: [models.KnowledgeType.CONCEPTUAL],
        Intent.PROCEDURAL: [models.KnowledgeType.PROCEDURAL],
        Intent.TROUBLESHOOTING: [models.KnowledgeType.PROCEDURAL],
        Intent.EXAMPLE_REQUEST: [models.KnowledgeType.ASSESSMENT],
        Intent.CONTEXT_REQUEST: [
            models.KnowledgeType.CONCEPTUAL,
            models.KnowledgeType.PROCEDURAL,
            models.KnowledgeType.ASSESSMENT,
        ],
    }

    DIFFICULTY_ORDER: dict[models.KnowledgeDifficulty, int] = {
        models.KnowledgeDifficulty.EASY: 0,
        models.KnowledgeDifficulty.MEDIUM: 1,
        models.KnowledgeDifficulty.HARD: 2,
    }

    def __init__(
        self,
        *,
        user_service: UserService,
        graph_rag: GraphRAG,
        hint_agent: Agent,
        rewrite_agent: Agent | None = None,
        content_rag: GraphRAG | None = None,
        ctf_reference: CTFReferenceService | None = None,
        top_k: int = 5,
        similarity_threshold: float = 0.85,
        hint_by_similarity_threshold: int = 2,
        confidence_threshold: float = 0.6,
    ):
        self.__user_service = user_service
        self.__graph_rag = graph_rag
        self.__content_rag = content_rag
        self.__ctf_reference = ctf_reference
        self.__hint_agent = hint_agent
        self.__rewrite_agent = rewrite_agent
        self.__top_k = top_k
        self.__similarity_threshold = similarity_threshold
        self.__hint_by_similarity_threshold = hint_by_similarity_threshold
        self.__confidence_threshold = confidence_threshold
        self.__logger = logging.getLogger(__name__)

    # Below this many messages, formatting the transcript verbatim is cheaper than
    # spending an extra LLM call to summarize it.
    __HISTORY_SUMMARY_TRIGGER = 4
    # Always keep the most recent exchange verbatim (in the student's own words)
    # even when older turns get summarized.
    __HISTORY_RECENT_VERBATIM = 2
    # A hint is a 1-2 sentence conceptual nudge, not a code review — it never
    # needs the full transcript or a verbatim pasted code block the way the
    # rewrite/answer agent does. Cap what reaches the hint agent so a long
    # student message or a short (not-yet-summarized) history doesn't get
    # resent in full just to produce a short nudge.
    __HINT_CONTEXT_CHAR_LIMIT = 400

    def __cap_for_hint(self, text: str) -> str:
        text = text.strip()
        if len(text) <= self.__HINT_CONTEXT_CHAR_LIMIT:
            return text
        return text[: self.__HINT_CONTEXT_CHAR_LIMIT].rstrip() + "…"

    @staticmethod
    def __format_history(message_history: list[LLMMessage] | None) -> str:
        """Render exchanges as plain text for inclusion in LLM prompts."""
        if not message_history:
            return ""
        lines = []
        for msg in message_history:
            role = "Student" if msg["role"] == "user" else "Tutor"
            lines.append(f"{role}: {msg['content']}")
        return "\n".join(lines)

    def __summarize_history(self, message_history: list[LLMMessage] | None) -> str:
        """Condense prior turns into a short context block, computed once per student
        turn and reused by every downstream call (hint agent, rewrite agent).

        Forwarding the full transcript to every agent call — and to GraphRAG, which
        internally re-summarizes it yet again just to build a retrieval query — pays
        for the same conversation history multiple times over. Short histories are
        cheap enough to include verbatim; longer ones are compressed with a single
        summarization call instead.
        """
        if not message_history:
            return ""

        if len(message_history) <= self.__HISTORY_SUMMARY_TRIGGER or not self.__rewrite_agent:
            recent = self.__format_history(message_history)
            return f"\n\nRecent conversation:\n{recent}" if recent else ""

        older = message_history[: -self.__HISTORY_RECENT_VERBATIM]
        recent = self.__format_history(message_history[-self.__HISTORY_RECENT_VERBATIM :])
        summary_prompt = (
            "Summarize this tutoring conversation in at most 60 words: what topics "
            "were covered, what specific facts/code/values were already revealed to "
            "the student, and where they seem stuck. Plain prose, no preamble.\n\n"
            f"{self.__format_history(older)}"
        )
        try:
            summary = self.__rewrite_agent.run_sync(summary_prompt).output.strip()
        except Exception as e:
            self.__logger.warning(f"History summarization failed, using recent turns only: {e}")
            return f"\n\nRecent conversation:\n{recent}" if recent else ""

        self.__logger.info(f"Summarized {len(older)} older messages: {summary}")
        return (
            f"\n\nConversation so far (summarized): {summary}"
            f"\n\nMost recent exchange:\n{recent}"
        )

    @staticmethod
    def __node_label_from_item(item) -> str:
        """Extract the KG node name from a retrieved item."""
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        # ContentChunk nodes tagged with their KG node ID (most accurate)
        if meta.get("kg_node_id"):
            return str(meta["kg_node_id"])
        # KG nodes (graph_rag) have 'name' in metadata
        if meta.get("name"):
            return str(meta["name"])
        # ContentChunk nodes without kg_node_id: use source_file reference
        source = meta.get("source_file", "")
        if source:
            base = source.rsplit("/", 1)[-1]
            page = meta.get("page")
            chunk_idx = meta.get("chunk_index")
            parts = [base]
            if page is not None:
                parts.append(f"p{page}")
            if chunk_idx is not None:
                parts.append(f"#{chunk_idx}")
            return " ".join(parts)
        if isinstance(item.content, str) and item.content.strip():
            return item.content.strip()[:60]
        return "Unknown"

    def __retrieve_node_metadata(
        self,
        query: str,
        *,
        graph_rag: GraphRAG | None = None,
    ):
        # Retrieval only — calls the retriever directly instead of GraphRAG.search(),
        # which would additionally spend an LLM call generating a draft answer from
        # these same chunks. That draft was never shown to the student; it was only
        # ever fed into __generate_response as a "reference answer" alongside the
        # very same chunks sent a second time. __generate_response already builds
        # the real answer from these chunks directly, so that draft call is pure
        # waste — both the extra round-trip and the doubled chunk tokens.
        rag = graph_rag or self.__graph_rag
        start_time = time.time()
        retriever_result = rag.retriever.search(query_text=query, top_k=self.__top_k)
        response_time_sec = round(time.time() - start_time, 2)
        self.__logger.info(f"Context retrieved in {response_time_sec} seconds")

        self.__logger.info("Parsing retriever results for node metadata...")
        retrieved_nodes, scores = [], []
        try:
            for item in retriever_result.items:
                retrieved_nodes.append(self.__node_label_from_item(item))
                if isinstance(item.metadata, dict):
                    scores.append(item.metadata.get("score"))
        except Exception as e:
            self.__logger.warning(f"Failed to parse retriever result metadata: {e}")

        self.__logger.info(f"Retrieved nodes: {len(retrieved_nodes)}")
        return retriever_result, retrieved_nodes, scores, response_time_sec

    @staticmethod
    def __looks_like_survey_request(query: str) -> bool:
        """Guard against students pasting graded survey/knowledge-test questions
        into the tutor chat instead of answering them independently.

        The per-challenge "Post-Challenge Knowledge Test" questions (see
        app/surveys/data.py) cover the exact same angr/CTF concepts this tutor is
        built to explain, and the survey is one modal click away on the same chat
        page — so a copy-pasted quiz question is retrieval-relevant and would
        otherwise sail straight through as a normal, answerable question. Matches
        either an explicit mention of the survey/test itself, or the structural
        shape of a pasted multiple-choice question (several "A) ..."/"B. ..."
        option lines), since students won't always name it as a "survey".
        """
        q_lower = query.lower()
        if any(kw in q_lower for kw in _SURVEY_KEYWORDS):
            return True
        return len(_MC_OPTION_LINE_RE.findall(query)) >= 2

    def __classify_intent(self, query: str) -> str:
        """Classify student query intent into categories.

        Returns one of: definition, procedural, troubleshooting, example_request, or context_request.
        """
        q_lower = query.lower().strip()
        for intent, keywords in self.INTENT_KEYWORDS.items():
            if any(kw in q_lower for kw in keywords):
                return intent
        return Intent.CONTEXT_REQUEST

    def __get_interaction_type(self, query: str):
        q_lower = query.lower()
        keywords_mapping = {
            "hint_request": ["hint", "help", "assist", "clue", "suggest"],
            "code_request": ["code", "script", "function", "write", "implement"],
            "concept_request": [
                "concept",
                "explain",
                "definition",
                "understand",
                "what is",
            ],
        }

        for interaction_type, keywords in keywords_mapping.items():
            if any(word in q_lower for word in keywords):
                return interaction_type
        return Intent.CONTEXT_REQUEST

    def __get_similar_trajectory_queries(
        self,
        query: str,
        *,
        user_id: str,
        course_id: str,
        challenge_stage: int | None,
    ):
        # Scoped to the current challenge: a question asked in an earlier challenge
        # must not count as a "repeat" here, or the student would walk into a brand
        # new challenge and immediately get the REPEATED-ASK/full-code treatment (or
        # a stale hint) meant for someone stuck on a different problem entirely.
        trajectories: list[str] = []

        # TODO: Why not only use the query_similarity as a exact match would be always present in the list?
        for item in self.__user_service.get_user_trajectory_by_query_exact_match(
            user_id, query, course_id=course_id, challenge_stage=challenge_stage
        ):
            if item.id and item.id not in trajectories:
                trajectories.append(item.id)

        for item in self.__user_service.get_user_trajectory_by_query_similarity(
            user_id, query, course_id=course_id, challenge_stage=challenge_stage,
            threshold=self.__similarity_threshold,
        ):
            if item.id and item.id not in trajectories:
                trajectories.append(item.id)

        return list(trajectories)

    def __generate_hint(
        self,
        query: str,
        query_repeat_count: int,
        *,
        retrieved_nodes: list[str],
        history_context: str = "",
    ):
        # The only hint trigger: the student has asked this same/similar question
        # (query_repeat_count, from __get_similar_trajectory_queries) at least
        # this many times already within this challenge.
        hint_triggered = query_repeat_count >= self.__hint_by_similarity_threshold
        hint_reason = None
        hint_text = None

        if hint_triggered:
            hint_query = self.__cap_for_hint(query)
            hint_history_context = self.__cap_for_hint(history_context) if history_context else ""
            hint_reason = "Repeated query (possible confusion)"
            hint_prompt = (
                f"In 1-2 short sentences, give an encouraging hint to help the student "
                f"progress on: '{hint_query}'. Focus on conceptual reinforcement rather than "
                f"giving the answer directly. Be brief — no preamble, no restating the question. "
                f"Context nodes: {retrieved_nodes[:3]}"
                f"{hint_history_context}"
            )
            hint_text = self.__hint_agent.run_sync(hint_prompt).output.strip()
            self.__logger.info(f"Hint triggered: {hint_reason} -> {hint_text}")

        return hint_triggered, hint_reason, hint_text


    @staticmethod
    def __scaffold_from_chunks(
        retriever_items: list,
        query_repeat_count: int,
    ) -> str | None:
        """Build a response directly from retrieved chunks without any LLM call.

        Returns None if there isn't enough structured content to build a response.
        Used as a fallback when the rewrite LLM is unreliable.
        """
        if not retriever_items:
            return None

        contents = []
        for item in retriever_items[:8]:
            text = item.content if isinstance(item.content, str) else ""
            if text.strip():
                contents.append(text.strip())

        if not contents:
            return None

        # Separate step chunks (contain " — Code: ") from descriptive chunks
        step_chunks = [c for c in contents if " — Code: " in c]
        desc_chunks = [c for c in contents if " — Code: " not in c]

        def _format_step(chunk: str) -> str:
            """Turn 'Step N: name — Hint: x — Code: y' into a readable block."""
            parts = {p.split(": ", 1)[0]: p.split(": ", 1)[1]
                     for p in chunk.split(" — ") if ": " in p}
            lines = []
            # Step header is everything before first " — "
            header = chunk.split(" — ")[0]
            lines.append(f"**{header}**")
            if "Hint" in parts:
                lines.append(f"_{parts['Hint']}_")
            if "Code" in parts:
                lines.append(f"```python\n{parts['Code']}\n```")
            return "\n".join(lines)

        if query_repeat_count == 0:
            # First ask: concept only, no code
            intro = desc_chunks[0] if desc_chunks else contents[0]
            # Strip any trailing code references
            intro = intro.split(" — Code:")[0].strip()
            return (
                f"{intro}\n\n"
                "Try to think about what the high-level goal of hooking is before looking at the steps. "
                "What problem does it solve?"
            )

        elif query_repeat_count == 1:
            # Second ask: show first step only
            if step_chunks:
                return (
                    "Since you've asked about this before, here's the first concrete step:\n\n"
                    + _format_step(step_chunks[0])
                    + "\n\nCan you figure out what the next step would be?"
                )
            return desc_chunks[0] if desc_chunks else contents[0]

        else:
            # Repeated ask: all steps verbatim
            if step_chunks:
                steps_text = "\n\n".join(_format_step(c) for c in step_chunks)
                return (
                    "Here are all the steps from the course material:\n\n"
                    + steps_text
                    + "\n\nRun through each step and come back if you hit an issue."
                )
            return "\n\n".join(contents[:5])

    @staticmethod
    def __trim_ground_truth(ground_truth: str, query_repeat_count: int) -> str:
        """Strip the solution code block out of the ground-truth guide on the first ask.

        The scaffolding rules below already forbid showing full code at that stage,
        so sending the guide's full solution script anyway — as happened on every
        single turn regardless of scaffolding level — just spends tokens on content
        the model is simultaneously instructed to ignore.
        """
        if query_repeat_count > 0:
            return ground_truth
        return _CODE_FENCE_RE.sub(
            "*(solution code withheld until a later attempt)*", ground_truth
        ).strip()

    def __generate_response(
        self,
        query: str,
        history_context: str = "",
        query_repeat_count: int = 0,
        retriever_items: list | None = None,
        ground_truth: str | None = None,
    ) -> str:
        if not self.__rewrite_agent or not retriever_items:
            return self.RESPONSE_FALLBACK

        # Build a grounding block from raw retrieved chunks so the LLM
        # cannot hallucinate code or facts that aren't in the course material.
        source_block = ""
        if retriever_items:
            chunks = []
            for item in retriever_items[:5]:
                content = item.content if isinstance(item.content, str) else str(item.content)
                if content.strip():
                    chunks.append(content.strip())
            if chunks:
                source_block = (
                    "\n\n## Course material retrieved from the knowledge graph "
                    "(ONLY use facts and code from here — do NOT invent anything):\n"
                    + "\n---\n".join(chunks)
                )

        # The knowledge graph only encodes a subset of the full course content and can be
        # incomplete or stale for a given challenge. When we have the complete, hand-verified
        # guide section for the student's current challenge, include it as the higher-priority
        # source of truth so the model can catch and correct any KG/ground-truth mismatch.
        ground_truth_block = ""
        if ground_truth:
            ground_truth = self.__trim_ground_truth(ground_truth, query_repeat_count)
            ground_truth_block = (
                "\n\n## Ground-truth reference for this challenge "
                "(complete and verified — this OUTRANKS the course material above):\n"
                + ground_truth
            )

        if query_repeat_count == 0:
            scaffolding_instruction = (
                "## Scaffolding Level: FIRST ASK\n"
                "The student is asking about this topic for the first time.\n"
                "- Explain the CONCEPT behind the answer — what it is and why it matters.\n"
                "- Give a high-level direction without revealing specific values, addresses, or full code.\n"
                "- Ask a guiding question to prompt the student to think further.\n"
                "- Do NOT show complete code snippets yet.\n"
                "Example style: 'The bitvector size needs to match the input field size. "
                "If scanf reads an 8-byte string, how many bits would that be?'"
            )
        elif query_repeat_count == 1:
            scaffolding_instruction = (
                "## Scaffolding Level: SECOND ASK\n"
                "The student has asked about this before and may be stuck.\n"
                "- Acknowledge their prior attempt naturally.\n"
                "- Reveal ONE specific step from the course material with its exact code snippet.\n"
                "- Explain what that code does so they understand, not just copy.\n"
                "- Still withhold the complete end-to-end solution.\n"
                "Example style: 'Since each password is 8 bytes, the bitvector should be "
                "8*8 = 64 bits: claripy.BVS(\"password0\", 8*8). Can you apply this to the other passwords?'"
            )
        else:
            scaffolding_instruction = (
                "## Scaffolding Level: REPEATED ASK\n"
                "The student has asked about this multiple times and is clearly stuck.\n"
                "- Be direct: name the specific fix, not just a conceptual nudge.\n"
                "- If code is needed, give ONLY the line(s) that need to change — reproduced "
                "EXACTLY as they appear in the course material above, do NOT paraphrase, "
                "simplify, or modify. Do NOT rewrite or restate their entire script.\n"
                "- Do not walk through the whole file line by line — one short note on why "
                "the fix works is enough.\n"
                "- Encourage them to run it and come back if they hit another issue.\n"
            )

        prompt = (
            "You are a Socratic teaching assistant for a CTF/binary analysis course.\n\n"
            f"{scaffolding_instruction}\n\n"
            "General rules (always apply):\n"
            "- Scope: this CTF/angr course only. Off-topic or unrelated-task requests → "
            "decline, redirect to the challenge.\n"
            "- Never answer survey/quiz/knowledge-test questions, even paraphrased — "
            "decline and point them to answer independently.\n"
            "- NEVER invent code, function names, or APIs. Only use code from the course material above.\n"
            "- Never mention: nodes, graph, knowledge graph, concept node, procedural node, "
            "assessment node, retrieval, scores, labels, edges, PREREQUISITE_FOR, DEPENDS_ON, "
            "EXTENDS_TO, ENABLES, or any other graph relationship names.\n"
            "- Never reference internal system structure or metadata.\n"
            "- Write as if you are a human tutor speaking directly to the student.\n"
            "- Do NOT start your response with phrases like 'Here is a rewritten answer' "
            "or any similar meta-commentary. Just give the answer directly.\n"
            "- This is a hint-driven tutor, not a tutorial site: give the shortest answer "
            "that resolves the specific question, never a complete write-up. No preamble, "
            "no restating the question, no repeating context the student already has.\n"
            "- Never structure your answer with multiple headers/sections (no 'Complete "
            "Fix', 'What Each Line Does', 'How to Get Your Addresses', 'Why It Was Slow', "
            "'Next Steps', etc.) and never re-paste the student's entire script back at "
            "them — at most one short, targeted code snippet.\n"
            "- Target a few sentences. If you find yourself writing more than that plus "
            "one snippet, you are over-explaining — cut it down to the one thing that "
            "actually answers the question.\n"
            + (
                "- Before answering, compare the course material against the ground-truth "
                "reference below. If they agree, use either. If they conflict on any concrete "
                "detail (addresses, register/variable names, function names, code, or steps), "
                "or the course material is missing something the ground-truth reference covers, "
                "trust the ground-truth reference and frame your answer using it instead — do "
                "not mix in the conflicting course-material detail.\n"
                if ground_truth_block
                else ""
            )
            + f"{source_block}"
            f"{ground_truth_block}"
            f"{history_context}\n\n"
            f"Student question: {query}"
        )
        result = self.__rewrite_agent.run_sync(prompt)
        return result.output.strip()

    def __rerank_results(
        self,
        items: list,
        intent: str,
        user_id: str,
    ) -> list:
        """Re-rank retriever results based on intent, difficulty, and pedagogical sequence.

        Uses KG node type metadata from Neo4j to determine node types.
        Returns the same items, reordered by pedagogical relevance.
        """
        if not items:
            return items

        preferred_types = self.INTENT_TO_PREFERRED_TYPES.get(
            intent,
            [
                models.KnowledgeType.CONCEPTUAL,
                models.KnowledgeType.PROCEDURAL,
                models.KnowledgeType.ASSESSMENT,
            ],
        )

        def _extract_node_type(item) -> str:
            metadata = item.metadata or {}
            node_type = metadata.get("node_type", "")
            if node_type:
                return node_type.lower()
            item_content = item.content if isinstance(item.content, str) else ""
            for t in models.KnowledgeType:
                if f"'type': '{t}'" in item_content or f'"type": "{t}"' in item_content:
                    return t
            return ""

        def _extract_difficulty(item) -> str:
            metadata = item.metadata or {}
            difficulty = metadata.get("difficulty", "")
            if difficulty:
                return difficulty.lower()
            item_content = item.content if isinstance(item.content, str) else ""
            for d in models.KnowledgeDifficulty:
                if (
                    f"'difficulty': '{d}'" in item_content
                    or f'"difficulty": "{d}"' in item_content
                ):
                    return d
            return models.KnowledgeDifficulty.MEDIUM

        def _extract_score(item) -> float:
            metadata = item.metadata or {}
            return metadata.get("score", 0.0) or 0.0

        def _rerank_key(item) -> tuple:
            node_type = _extract_node_type(item)
            difficulty = _extract_difficulty(item)
            score = _extract_score(item)

            # 1. Intent match: lower is better (0 = best match)
            if node_type in preferred_types:
                type_rank = preferred_types.index(node_type)
            else:
                type_rank = len(preferred_types)

            # 2. Difficulty: lower is easier
            difficulty_rank = self.DIFFICULTY_ORDER.get(difficulty, 1)

            # 3. Pedagogical sequence: conceptual -> procedural -> assessment
            pedagogical_order = {
                models.KnowledgeType.CONCEPTUAL: 0,
                models.KnowledgeType.PROCEDURAL: 1,
                models.KnowledgeType.ASSESSMENT: 2,
            }
            pedagogical_rank = pedagogical_order.get(node_type, 3)

            return (type_rank, difficulty_rank, pedagogical_rank, -score)

        return sorted(items, key=_rerank_key)

    def retrieve_context(
        self,
        user_id: str,
        query: str,
        course_id: str,
        message_history: list[LLMMessage] | None = None,
        challenge_stage: int | None = None,
    ):
        try:
            self.__logger.info(f"Loading user {user_id} state...")
            user = self.__user_service.get_user(user_id)
            if not user:
                self.__logger.warning(f"User {user_id} not found!")
                return None
            if not user.id:
                self.__logger.warning(f"User {user_id} has no ID!")
                return None

            if self.__looks_like_survey_request(query):
                self.__logger.info(
                    f"Declining survey/knowledge-test-like request from user {user_id}: '{query}'"
                )
                return SupervisorResult(
                    answer=self.SURVEY_DECLINE_RESPONSE, hint_text=None, hint_reason=None
                )

            self.__logger.info("Retrieving node metadata...")
            retriever_result, retrieved_nodes, scores, response_time_sec = (
                self.__retrieve_node_metadata(query)
            )
            node_entry_count = len(retrieved_nodes)

            # Check confidence threshold; if low or no results, try the secondary RAG
            # source (if one is configured) before giving up.
            top_score = scores[0] if scores else None
            low_confidence = not retrieved_nodes or top_score is None or top_score < self.__confidence_threshold
            if low_confidence:
                self.__logger.warning(
                    f"RAG retrieval insufficient (nodes={len(retrieved_nodes)}, score={top_score}): '{query}'"
                )
                if self.__content_rag:
                    self.__logger.info("Trying secondary RAG source with original query...")
                    cr, cn, cs, ct = self.__retrieve_node_metadata(
                        query, graph_rag=self.__content_rag
                    )
                    if cn:
                        self.__logger.info(f"Secondary RAG source succeeded (nodes={len(cn)}, score={cs[0] if cs else None})")
                        retriever_result, retrieved_nodes, scores, response_time_sec = cr, cn, cs, ct
                        node_entry_count = len(retrieved_nodes)
                    else:
                        self.__logger.warning("Secondary RAG source also returned no results.")
                        return SupervisorResult(answer=self.RESPONSE_FALLBACK, hint_text=None, hint_reason=None)
                else:
                    return SupervisorResult(answer=self.RESPONSE_FALLBACK, hint_text=None, hint_reason=None)

            self.__logger.info("Determining interaction type...")
            interaction_type = self.__classify_intent(query)

            # Re-rank retriever results based on intent, difficulty, and pedagogical sequence
            if retriever_result.items:
                retriever_result.items = self.__rerank_results(
                    retriever_result.items,
                    interaction_type,
                    user.id,
                )
                # Recompute node names and scores after re-ranking
                retrieved_nodes = []
                scores = []
                for item in retriever_result.items:
                    retrieved_nodes.append(self.__node_label_from_item(item))
                    if isinstance(item.metadata, dict):
                        scores.append(item.metadata.get("score"))

            self.__logger.info("Computing node count and repeat count...")
            similar_trajectory_ids = self.__get_similar_trajectory_queries(
                query, user_id=user.id, course_id=course_id, challenge_stage=challenge_stage
            )
            query_repeat_count = len(similar_trajectory_ids)
            self.__logger.info(f"Query repeat count: {query_repeat_count} (similar trajectories: {len(similar_trajectory_ids)})")

            # Summarized once here and reused below — every downstream call used to
            # separately re-send (or, in GraphRAG's case, re-summarize) the same full
            # transcript on its own.
            history_context = self.__summarize_history(message_history)

            hint_triggered, hint_reason, hint_text = self.__generate_hint(
                query,
                query_repeat_count,
                retrieved_nodes=retrieved_nodes,
                history_context=history_context,
            )

            retriever_items = retriever_result.items if retriever_result else None
            ground_truth = (
                self.__ctf_reference.get_reference(challenge_stage)
                if self.__ctf_reference
                else None
            )
            if ground_truth:
                self.__logger.info(
                    f"Ground-truth reference found for challenge_stage={challenge_stage}; "
                    "will take priority over KG material on conflict."
                )
            answer = self.__generate_response(
                query, history_context, query_repeat_count,
                retriever_items=retriever_items,
                ground_truth=ground_truth,
            )

            new_trajectory = models.UserTrajectory(
                user_id=user.id,
                query=query,
                retrieved_nodes=retrieved_nodes,
                scores=scores,
                interaction_type=interaction_type,
                query_repeat_count=query_repeat_count + 1,
                node_entry_count=node_entry_count,
                response_time_sec=response_time_sec,
                hint_triggered=hint_triggered,
                hint_reason=hint_reason,
                hint_text=hint_text,
                course_id=course_id,
                challenge_stage=challenge_stage,
            )
            self.__user_service.add_trajectory_entry(user_id, new_trajectory)
            self.__logger.info(
                f"Context retrieval logged. ({interaction_type}, {node_entry_count} nodes, {response_time_sec}s)"
            )
            return SupervisorResult(
                answer=answer,
                hint_text=hint_text,
                hint_reason=hint_reason,
            )

        except Exception as e:
            self.__logger.error(f"Error occurred while retrieving context: {e}")
            return None
