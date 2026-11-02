import pytest
from neo4j_graphrag.generation import GraphRAG
from neo4j_graphrag.llm.base import LLMInterface
from neo4j_graphrag.retrievers import VectorRetriever


@pytest.fixture
def mock_graphrag(mocker):
    mock_retriever = mocker.MagicMock(spec=VectorRetriever)
    mock_llm = mocker.MagicMock(spec=LLMInterface)
    instance = GraphRAG(retriever=mock_retriever, llm=mock_llm)
    # spec'd against the instance (not the class) so `.retriever` is exposed —
    # supervisor_agent calls rag.retriever.search(...) directly now, bypassing
    # GraphRAG.search()'s LLM-generation step entirely.
    mock = mocker.MagicMock(spec=instance)
    mock.retriever = mock_retriever
    return mock
