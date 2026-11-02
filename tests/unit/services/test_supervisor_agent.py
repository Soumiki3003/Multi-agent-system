import pytest
from unittest.mock import MagicMock
from app.services.supervisor_agent import SupervisorAgentService


@pytest.fixture
def supervisor_agent(mock_user_service, mock_graphrag):
    return SupervisorAgentService(
        user_service=mock_user_service,
        graph_rag=mock_graphrag,
        hint_agent=MagicMock(),
        rewrite_agent=MagicMock(),
    )


def test_retrieve_context_user_not_found(supervisor_agent, mock_user_service):
    mock_user_service.get_user.side_effect = lambda uid: None
    res = supervisor_agent.retrieve_context("u1", "what is this?", "c1")
    assert res is None


def test_retrieve_context_no_user_id(supervisor_agent, mock_user_service):
    mock_user = MagicMock()
    mock_user.id = None
    mock_user_service.get_user.side_effect = lambda uid: mock_user
    res = supervisor_agent.retrieve_context("u1", "what is this?", "c1")
    assert res is None


def test_retrieve_context_success(supervisor_agent, mock_user_service, mock_graphrag, user_trajectory):
    mock_user = MagicMock()
    mock_user.id = "u1"
    mock_user_service.get_user.side_effect = lambda uid: mock_user
    mock_user_service.add_trajectory_entry.side_effect = lambda *a: user_trajectory

    item = MagicMock()
    item.content = "{'name': 'TestNode'}"
    item.metadata = {"score": 0.9}
    mock_retriever_result = MagicMock()
    mock_retriever_result.items = [item]

    mock_graphrag.retriever.search.side_effect = lambda *a, **kw: mock_retriever_result

    t1 = MagicMock()
    t1.id = "t1"
    mock_user_service.get_user_trajectory_by_query_exact_match.side_effect = lambda *a, **kw: [t1]
    mock_user_service.get_user_trajectory_by_query_similarity.side_effect = lambda *a, **kw: []

    t_hist = MagicMock()
    t_hist.query = "how to run the code"
    mock_user_service.get_user_trajectory.side_effect = lambda *a, **kw: [t_hist]

    mock_hint_out = MagicMock()
    mock_hint_out.output = "Here is a hint"
    supervisor_agent._SupervisorAgentService__hint_agent.run_sync.return_value = mock_hint_out
    supervisor_agent._SupervisorAgentService__rewrite_agent.run_sync.return_value = (
        MagicMock(output="Hello")
    )

    res = supervisor_agent.retrieve_context("u1", "how do i run this", "c1")

    assert res is not None
    assert res.answer == "Hello"
    # Only one prior similar query (below the default repeat threshold of 2) —
    # no hint should be triggered yet.
    assert res.hint_text is None
    assert res.hint_reason is None


def test_retrieve_context_repeated_query(supervisor_agent, mock_user_service, mock_graphrag, user_trajectory):
    mock_user = MagicMock()
    mock_user.id = "u1"
    mock_user_service.get_user.side_effect = lambda uid: mock_user
    mock_user_service.add_trajectory_entry.side_effect = lambda *a: user_trajectory

    item = MagicMock()
    item.content = "{'name': 'TestNode'}"
    item.metadata = {"score": 0.9}
    mock_retriever_result = MagicMock()
    mock_retriever_result.items = [item]
    mock_graphrag.retriever.search.side_effect = lambda *a, **kw: mock_retriever_result

    t1 = MagicMock(); t1.id = "t1"
    t2 = MagicMock(); t2.id = "t2"
    t3 = MagicMock(); t3.id = "t3"
    mock_user_service.get_user_trajectory_by_query_exact_match.side_effect = lambda *a, **kw: [t1, t2]
    mock_user_service.get_user_trajectory_by_query_similarity.side_effect = lambda *a, **kw: [t2, t3]

    mock_hint_out = MagicMock()
    mock_hint_out.output = "Another Hint"
    supervisor_agent._SupervisorAgentService__hint_agent.run_sync.return_value = mock_hint_out
    supervisor_agent._SupervisorAgentService__rewrite_agent.run_sync.return_value = (
        MagicMock(output="Hello again")
    )

    res = supervisor_agent.retrieve_context("u1", "query", "c1")

    assert res.answer == "Hello again"
    assert res.hint_reason == "Repeated query (possible confusion)"
    assert res.hint_text == "Another Hint"


def test_retrieve_context_exception(supervisor_agent, mock_user_service):
    mock_user_service.get_user.side_effect = Exception("Boom")
    res = supervisor_agent.retrieve_context("u1", "query", "c1")
    assert res is None
