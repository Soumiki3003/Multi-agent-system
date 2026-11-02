import logging.config
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

try:
    import logfire
except ImportError:
    logfire = None

from dependency_injector import containers, providers
from jinja2 import Environment, FileSystemLoader
from pydantic_ai import Agent, Embedder

from app import prompts

from . import controllers, gateways, services


@contextmanager
def folder(folderpath: str) -> Generator[Path, None, None]:
    upload_dir = Path(folderpath)
    upload_dir.mkdir(parents=True, exist_ok=True)
    if not upload_dir.is_dir():
        raise NotADirectoryError(f"Upload path {folderpath} is not a valid directory.")
    yield upload_dir


@contextmanager
def setup_logs(config: dict):
    if logfire is not None:
        logfire.configure()
        logfire.instrument_pydantic_ai()

    root_log = logging.config.dictConfig(config)
    yield root_log


_ROOT_FOLDER = Path(__file__).parent
_TEMPLATES_FOLDER = _ROOT_FOLDER / "templates"
_STATIC_FOLDER = _ROOT_FOLDER / "static"
_UPLOADS_FOLDER = _STATIC_FOLDER / "uploads"
_CTF_REFERENCE_FILE = _ROOT_FOLDER.parent / "angr_summary.md"


class Core(containers.DeclarativeContainer):
    config = providers.Configuration()

    logging = providers.Resource(setup_logs, config=config.logging)
    static_folder = providers.Resource(folder, folderpath=_STATIC_FOLDER)
    uploads_folder = providers.Resource(folder, folderpath=_UPLOADS_FOLDER)
    allowed_extensions = config.allowed_extensions.as_(
        lambda v: v.split(",") if v else []
    )

    web_templates_folder = _TEMPLATES_FOLDER / "web"


class AI(containers.DeclarativeContainer):
    config = providers.Configuration()

    # The app serves requests one at a time (see main.py); an LLM call that hangs
    # would otherwise block every other student indefinitely instead of just
    # failing that one request.
    _AGENT_TIMEOUT_SECONDS = 30

    prompt_template_env = providers.Singleton(
        Environment,
        loader=FileSystemLoader(_TEMPLATES_FOLDER / "prompts"),
        autoescape=True,
    )

    default_agent = providers.Singleton(
        Agent,
        model=config.agents.default.model.required(),
        # Hint and rewrite/answer generation both run through this agent — cap
        # output so a "repeated ask" can't balloon into a multi-section essay.
        model_settings={"max_tokens": 600, "timeout": _AGENT_TIMEOUT_SECONDS},
        retries=3,
    )

    default_embedder = providers.Singleton(
        Embedder,
        model=config.embedder.default.model.required(),
    )

    knowledge_file_agent = providers.Singleton(
        Agent,
        model=config.agents.knowledge_file.model.required(),
        system_prompt=providers.Singleton(
            prompts.get_knowledge_system_prompt,
            env=prompt_template_env,
        ),
        model_settings={"timeout": _AGENT_TIMEOUT_SECONDS},
        retries=3,
    )

    graph_agent = providers.Singleton(
        Agent,
        model=config.agents.graph.model.required(),
        # Powers GraphRAG's internal answer draft (never shown to the student —
        # it's rewritten by supervisor_agent before reaching them), so it never
        # needs much room either.
        model_settings={"max_tokens": 600, "timeout": _AGENT_TIMEOUT_SECONDS},
        retries=3,
    )


class Gateways(containers.DeclarativeContainer):
    config = providers.Configuration()
    ai = providers.DependenciesContainer()

    neo4j_driver = providers.Resource(
        gateways.neo4j_driver,
        uri=config.neo4j.uri,
        user=config.neo4j.user,
        password=config.neo4j.password,
    )

    neo4j_session = providers.Factory(
        gateways.neo4j_session,
        driver=neo4j_driver,
    )

    neo4j_agent = providers.Singleton(
        gateways.Neo4jAgent.from_pydantic_agent,
        agent=ai.graph_agent,
    )

    neo4j_embedder = providers.Singleton(
        gateways.Neo4jEmbedder,
        embedder=ai.default_embedder,
    )

    # TODO: Add a prompt template
    trajectory_graphrag = providers.Resource(
        gateways.neo4j_graphrag,
        params=providers.Factory(
            gateways.Neo4jGraphRAGParams,
            driver=neo4j_driver,
            llm=neo4j_agent,
            embedder=neo4j_embedder,
            vector_index=providers.Factory(
                gateways.Neo4jGraphRAGVectorIndexParams,
                name=config.rag.indexes.trajectory.vector.name.required(),
                label=config.rag.indexes.trajectory.vector.label.required(),
                field_name=config.rag.indexes.trajectory.vector.field_name.required(),
            ),
            fulltext_index=providers.Factory(
                gateways.Neo4jGraphRAGFulltextIndexParams,
                name=config.rag.indexes.trajectory.text.name.required(),
                label=config.rag.indexes.trajectory.text.label.required(),
                node_properties=config.rag.indexes.trajectory.text.fields.required(),
            ),
        ),
    )

    content_chunk_graphrag = providers.Resource(
        gateways.neo4j_graphrag,
        params=providers.Factory(
            gateways.Neo4jGraphRAGParams,
            driver=neo4j_driver,
            llm=neo4j_agent,
            embedder=neo4j_embedder,
            vector_index=providers.Factory(
                gateways.Neo4jGraphRAGVectorIndexParams,
                name=config.rag.indexes.content_chunk.vector.name.required(),
                label=config.rag.indexes.content_chunk.vector.label.required(),
                field_name=config.rag.indexes.content_chunk.vector.field_name.required(),
            ),
        ),
    )


class Services(containers.DeclarativeContainer):
    config = providers.Configuration()

    core = providers.DependenciesContainer()
    ai = providers.DependenciesContainer()
    gateways = providers.DependenciesContainer()

    file = providers.Factory(services.FileService)
    auth = providers.Singleton(services.AuthService)
    knowledge_upload = providers.Singleton(services.KnowledgeUploadService)
    knowledge = providers.Factory(
        services.KnowledgeService,
        session_factory=gateways.neo4j_session.provider,
        agent=ai.knowledge_file_agent,
        template_env=ai.prompt_template_env,
        file_service=file,
        upload_service=knowledge_upload,
        static_folder=core.static_folder,
        embedder=ai.default_embedder,
    )
    user = providers.Factory(
        services.UserService,
        session_factory=gateways.neo4j_session.provider,
        embedder=ai.default_embedder,
        rag=gateways.trajectory_graphrag,
        auth_service=auth,
        trajectory_vector_index_field="trajectory_vector",
        trajectory_full_text_index_field="trajectory_text",
    )
    chat = providers.Factory(
        services.ChatService,
        session_factory=gateways.neo4j_session.provider,
    )
    ctf_reference = providers.Singleton(
        services.CTFReferenceService,
        file_path=str(_CTF_REFERENCE_FILE),
    )
    supervisor_agent = providers.Factory(
        services.SupervisorAgentService,
        user_service=user,
        # Content-chunk KG is the sole retrieval source for answer generation —
        # trajectory_graphrag stays wired to `user` (above) for repeat-question/hint
        # detection only. It must never be plugged in here: UserTrajectory nodes have
        # no `content` property, so the retriever's result_formatter falls back to
        # str(node), which dumps the node's full embedding vector into the LLM prompt.
        graph_rag=gateways.content_chunk_graphrag,
        ctf_reference=ctf_reference,
        hint_agent=ai.default_agent,
        rewrite_agent=ai.default_agent,
        similarity_threshold=0.70,
        hint_by_similarity_threshold=2,
    )
    dashboard = providers.Factory(
        services.DashboardService,
        session_factory=gateways.neo4j_session.provider,
    )


class Controllers(containers.DeclarativeContainer):
    config = providers.Configuration()
    core = providers.DependenciesContainer()
    services = providers.DependenciesContainer()

    knowledge_controller = providers.Factory(
        controllers.KnowledgeController,
        knowledge_service=services.knowledge,
        uploads_service=services.knowledge_upload,
    )
    auth_controller = providers.Factory(
        controllers.AuthController,
        user_service=services.user,
        auth_service=services.auth,
    )
    course_controller = providers.Factory(
        controllers.CourseController,
        knowledge_service=services.knowledge,
        user_service=services.user,
        chat_service=services.chat,
        supervisor_agent_service=services.supervisor_agent,
        uploads_folder=core.uploads_folder,
        uploads_service=services.knowledge_upload,
    )


class Application(containers.DeclarativeContainer):
    config = providers.Configuration(
        yaml_files=[Path(__file__).parents[1] / "config.yaml"], strict=True
    )

    core = providers.Container(Core, config=config.core)
    ai = providers.Container(AI, config=config.ai)
    gateways = providers.Container(Gateways, config=config.gateways, ai=ai)

    services = providers.Container(
        Services,
        config=config.services,
        core=core,
        ai=ai,
        gateways=gateways,
    )
    controllers = providers.Container(
        Controllers,
        config=config.controllers,
        core=core,
        services=services,
    )
