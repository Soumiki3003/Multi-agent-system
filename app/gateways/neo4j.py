import asyncio
import json
import logging
from contextlib import contextmanager
from typing import Any, Literal, Self, Sequence

from neo4j import Driver, GraphDatabase
from neo4j_graphrag.embeddings.base import Embedder as EmbedderInterface
from neo4j_graphrag.generation import GraphRAG
from neo4j_graphrag.generation.prompts import RagTemplate
from neo4j_graphrag.indexes import create_fulltext_index, create_vector_index
from neo4j_graphrag.llm.base import LLMInterface
from neo4j_graphrag.llm.types import LLMResponse, ToolCall, ToolCallResponse
from neo4j_graphrag.message_history import MessageHistory
from neo4j_graphrag.retrievers import VectorRetriever
from neo4j_graphrag.types import RetrieverResultItem
from neo4j_graphrag.tool import Tool
from neo4j_graphrag.types import LLMMessage
from neo4j_graphrag.utils.rate_limit import RateLimitHandler
from pydantic import BaseModel, ConfigDict, PositiveInt
from pydantic_ai import (
    Agent,
    AgentRunResult,
    Embedder,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai import (
    Tool as PydanticAiTool,
)

# GraphRAG's default system instructions ("Answer the user question using the
# provided context.") give the LLM no reason to keep this short — this answer
# is only ever an internal draft consumed by SupervisorAgentService's rewrite
# step, never shown to the student, so there's no reason to pay for a long,
# formatted one.
_RAG_SYSTEM_INSTRUCTIONS = (
    "Answer the user's question using ONLY the provided context — do not invent "
    "facts or code. This is an internal draft that will be rewritten before a "
    "student ever sees it, so prioritize brevity over completeness: a few plain "
    "sentences, plus one short code snippet only if truly necessary. No markdown "
    "headers, no emoji, no multiple sections."
)


@contextmanager
def neo4j_driver(uri: str, *, user: str, password: str):
    logger = logging.getLogger(__name__)
    logger.info(f"Connecting to Neo4j at {uri} with user {user}")
    auth = (user, password) if user and password else None
    try:
        driver = GraphDatabase.driver(uri, auth=auth)
        logger.info(f"Successfully connected to Neo4j at {uri}")
        yield driver
    except Exception as e:
        logger.error(f"Failed to connect to Neo4j at {uri}: {e}")
        raise
    else:
        driver.close()
        logger.info("Neo4j driver closed")


@contextmanager
def neo4j_session(driver: Driver, **session_kwargs):
    logger = logging.getLogger(__name__)
    logger.debug(f"Opening Neo4j session with kwargs: {session_kwargs}")
    try:
        with driver.session(**session_kwargs) as session:
            logger.debug("Neo4j session opened successfully")
            yield session
    except Exception as e:
        logger.error(f"Error during Neo4j session: {e}")
        raise
    finally:
        logger.debug("Neo4j session closed")


class Neo4jGraphRAGVectorIndexParams(BaseModel):
    name: str
    label: str
    dimension: PositiveInt | None = None
    field_name: str = "vector"
    similarity_fn: Literal["euclidean", "cosine"] = "cosine"


class Neo4jGraphRAGFulltextIndexParams(BaseModel):
    name: str
    label: str
    node_properties: list[str]


class Neo4jGraphRAGParams(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    driver: Driver
    llm: LLMInterface
    embedder: EmbedderInterface
    database: str | None = None

    vector_index: Neo4jGraphRAGVectorIndexParams
    fulltext_index: Neo4jGraphRAGFulltextIndexParams | None = None


def neo4j_graphrag(params: Neo4jGraphRAGParams):
    logger = logging.getLogger(__name__)
    logger.info("Initializing Neo4j GraphRAG")

    if params.vector_index.dimension is None:
        logger.debug("Vector index dimension not specified, auto-detecting...")
        params.vector_index.dimension = len(
            params.embedder.embed_query("let me know your dimension")
        )
        logger.debug(f"Detected vector dimension: {params.vector_index.dimension}")

    logger.info(f"Creating vector index: {params.vector_index.name}")
    create_vector_index(
        params.driver,
        params.vector_index.name,
        params.vector_index.label,
        params.vector_index.field_name,
        params.vector_index.dimension,
        params.vector_index.similarity_fn,
        fail_if_exists=False,
        neo4j_database=params.database,
    )
    logger.info(f"Vector index '{params.vector_index.name}' created/verified")

    if fulltext_index := params.fulltext_index:
        logger.info(f"Creating fulltext index: {fulltext_index.name}")
        create_fulltext_index(
            params.driver,
            fulltext_index.name,
            fulltext_index.label,
            fulltext_index.node_properties,
            fail_if_exists=False,
            neo4j_database=params.database,
        )
        logger.info(f"Fulltext index '{fulltext_index.name}' created/verified")

    def _result_formatter(record) -> RetrieverResultItem:
        """Put all node properties into metadata so callers can access kg_node_id etc."""
        node = record.get("node")
        score = record.get("score")
        if node:
            node_data = dict(node)
            return RetrieverResultItem(
                content=node_data.get("content") or str(node),
                metadata={**node_data, "score": score},
            )
        return RetrieverResultItem(content="", metadata={"score": score})

    logger.debug("Initializing VectorRetriever")
    retriever = VectorRetriever(
        params.driver,
        params.vector_index.name,
        params.embedder,
        result_formatter=_result_formatter,
    )
    logger.info("Neo4j GraphRAG initialized successfully")
    return GraphRAG(
        retriever=retriever,
        llm=params.llm,
        prompt_template=RagTemplate(system_instructions=_RAG_SYSTEM_INSTRUCTIONS),
    )


class Neo4jAgent(LLMInterface):
    def __init__(
        self,
        model_name: str,
        model_params: dict[str, Any] | None = None,
        rate_limit_handler: RateLimitHandler | None = None,
        **kwargs: Any,
    ):
        self.__logger = logging.getLogger(__name__)
        super().__init__(model_name=model_name, model_params=model_params, **kwargs)

    @classmethod
    def from_pydantic_agent(cls, agent: Agent) -> Self:
        if not agent.model:
            raise ValueError("Agent model is required")

        model_name = (
            agent.model if isinstance(agent.model, str)
            else getattr(agent.model, "model_id", None) or agent.model.model_name
        )
        model_kwargs = agent.model_settings

        return cls(model_name=model_name, model_params=model_kwargs)

    async def __run_agent(
        self,
        agent: Agent,
        input: list[LLMMessage],
        message_history: list[LLMMessage] | MessageHistory | None = None,
    ) -> AgentRunResult:
        parsed_input: list[UserContent] = [
            message["content"] for message in input if message["role"] == "user"
        ]
        parsed_message_history: list[ModelMessage] = []
        for message in (
            message_history.messages
            if isinstance(message_history, MessageHistory)
            else (message_history or [])
        ):
            role = message["role"]
            content = message["content"]

            if role == "system":
                agent_message = ModelRequest(
                    parts=[SystemPromptPart(content)],
                )
            elif role == "user":
                agent_message = ModelRequest(
                    parts=[UserPromptPart(content)],
                )
            elif role == "assistant":
                agent_message = ModelResponse(
                    parts=[TextPart(content)],
                )
            else:
                raise ValueError(f"Invalid message role: {role}")
            parsed_message_history.append(agent_message)

        return await self._rate_limit_handler.handle_async(agent.run)(
            parsed_input,
            message_history=parsed_message_history,
        )

    def invoke(
        self,
        input: str,
        message_history: list[LLMMessage] | MessageHistory | None = None,
        system_instruction: str | None = None,
    ) -> LLMResponse:
        return asyncio.run(self.ainvoke(input, message_history, system_instruction))

    async def ainvoke(
        self,
        input: str,
        message_history: list[LLMMessage] | MessageHistory | None = None,
        system_instruction: str | None = None,
    ) -> LLMResponse:
        self.__logger.debug(
            f"Neo4jAgent ainvoke called with input length: {len(input)}"
        )
        agent = Agent(
            self.model_name,
            system_prompt=system_instruction or (),
            model_settings=ModelSettings(**self.model_params) or None,
        )
        parsed_input = [LLMMessage(role="user", content=input)]

        self.__logger.debug("Running agent for ainvoke")
        result = await self.__run_agent(agent, parsed_input, message_history)
        self.__logger.debug(
            f"Agent completed, output length: {len(str(result.output))}"
        )
        return LLMResponse(content=result.output)

    async def ainvoke_with_tools(
        self,
        input: str,
        tools: Sequence[Tool],
        message_history: list[LLMMessage] | MessageHistory | None = None,
        system_instruction: str | None = None,
    ) -> ToolCallResponse:
        supported_tools = [
            PydanticAiTool.from_schema(
                function=tool.execute,
                name=tool.get_name(),
                description=tool.get_description(),
                json_schema=tool.get_parameters(),
            )
            for tool in tools
        ]

        agent = Agent(
            model=self.model_name,
            model_settings=ModelSettings(**self.model_params) or None,
            system_prompt=system_instruction or (),
            tools=supported_tools,
        )

        result = await self.__run_agent(
            agent, [LLMMessage(role="user", content=input)], message_history
        )
        return ToolCallResponse(
            content=result.output,
            tool_calls=[
                ToolCall(
                    name=part.tool_name,
                    arguments=json.loads(json.dumps(part.args or "{}")),
                )
                for message in result.all_messages()
                for part in message.parts
                if isinstance(part, ToolCallPart)
            ],
        )

    def invoke_with_tools(
        self,
        input: str,
        tools: Sequence[Tool],
        message_history: list[LLMMessage] | MessageHistory | None = None,
        system_instruction: str | None = None,
    ) -> ToolCallResponse:
        return asyncio.run(
            self.ainvoke_with_tools(input, tools, message_history, system_instruction)
        )


class Neo4jEmbedder(EmbedderInterface):
    def __init__(self, embedder: Embedder) -> None:
        self.__embedder = embedder
        super().__init__()

    def embed_query(self, text: str) -> list[float]:
        return list(
            self._rate_limit_handler.handle_sync(self.__embedder.embed_documents_sync)(
                text
            ).embeddings[0]
        )

    async def async_embed_query(self, text: str) -> list[float]:
        result = await self._rate_limit_handler.handle_async(
            self.__embedder.embed_documents
        )(text)
        return list(result.embeddings[0])
