"""Tests for confidence threshold fallback — Concern #3a (TDD RED phase)."""

import logging

import pytest
from unittest.mock import MagicMock

from app.services.supervisor_agent import SupervisorAgentService


@pytest.fixture
def supervisor_agent_with_threshold(mock_user_service, mock_graphrag):
    return SupervisorAgentService(
        user_service=mock_user_service,
        graph_rag=mock_graphrag,
        hint_agent=MagicMock(),
        rewrite_agent=MagicMock(),
        confidence_threshold=0.6,
    )


@pytest.fixture
def _happy_path_setup(mock_user_service, mock_graphrag, user_trajectory, supervisor_agent_with_threshold):
    """Common setup: valid user, retriever with an item, no repeated queries."""
    mock_user = MagicMock()
    mock_user.id = "u1"
    mock_user_service.get_user.side_effect = lambda uid: mock_user
    mock_user_service.add_trajectory_entry.side_effect = lambda *a: user_trajectory

    item = MagicMock()
    item.content = "{'name': 'TestNode'}"

    mock_retriever_result = MagicMock()
    mock_retriever_result.items = [item]
    mock_graphrag.retriever.search.side_effect = lambda *a, **kw: mock_retriever_result

    supervisor_agent_with_threshold._SupervisorAgentService__rewrite_agent.run_sync.return_value = (
        MagicMock(output="Normal answer about the topic")
    )

    t1 = MagicMock()
    t1.id = "t1"
    mock_user_service.get_user_trajectory_by_query_exact_match.side_effect = (
        lambda *a, **kw: [t1]
    )
    mock_user_service.get_user_trajectory_by_query_similarity.side_effect = (
        lambda *a, **kw: []
    )

    t_hist = MagicMock()
    t_hist.query = "unrelated past query"
    mock_user_service.get_user_trajectory.side_effect = lambda *a, **kw: [t_hist]

    return item, mock_retriever_result


def test_scores_above_threshold_returns_normal_answer(
    supervisor_agent_with_threshold, _happy_path_setup
):
    """When scores[0] >= confidence_threshold, normal answer is returned."""
    item, _ = _happy_path_setup
    item.metadata = {"score": 0.85}

    svc = supervisor_agent_with_threshold
    svc._SupervisorAgentService__hint_agent.run_sync.return_value = MagicMock(output="")

    res = svc.retrieve_context("u1", "what is symbolic execution?", "c1")

    assert res is not None
    assert res.answer == "Normal answer about the topic"


def test_scores_below_threshold_returns_fallback(
    supervisor_agent_with_threshold, _happy_path_setup
):
    """When scores[0] < confidence_threshold, fallback message is returned instead."""
    item, mock_rag_result = _happy_path_setup
    item.metadata = {"score": 0.3}

    svc = supervisor_agent_with_threshold
    svc._SupervisorAgentService__hint_agent.run_sync.return_value = MagicMock(output="")

    res = svc.retrieve_context("u1", "what is symbolic execution?", "c1")

    assert res is not None
    assert res.answer == SupervisorAgentService.RESPONSE_FALLBACK


def test_low_confidence_query_is_logged(
    supervisor_agent_with_threshold, _happy_path_setup, caplog
):
    """Low-confidence queries are logged for instructor review."""
    item, mock_rag_result = _happy_path_setup
    item.metadata = {"score": 0.1}

    svc = supervisor_agent_with_threshold
    svc._SupervisorAgentService__hint_agent.run_sync.return_value = MagicMock(output="")

    with caplog.at_level(logging.WARNING):
        res = svc.retrieve_context("u1", "obscure question", "c1")

    assert res is not None
    assert res.answer == SupervisorAgentService.RESPONSE_FALLBACK
    assert any("RAG retrieval insufficient" in record.message for record in caplog.records)


def test_score_exactly_at_threshold_returns_normal_answer(
    supervisor_agent_with_threshold, _happy_path_setup
):
    """When scores[0] == confidence_threshold exactly, normal answer is returned."""
    item, _ = _happy_path_setup
    item.metadata = {"score": 0.6}

    svc = supervisor_agent_with_threshold
    svc._SupervisorAgentService__hint_agent.run_sync.return_value = MagicMock(output="")

    res = svc.retrieve_context("u1", "test query", "c1")

    assert res is not None
    assert res.answer == "Normal answer about the topic"


def test_no_scores_returns_fallback(
    supervisor_agent_with_threshold, mock_user_service, mock_graphrag, user_trajectory
):
    """No retrieved items means no scores, which is treated as low confidence —
    fallback message is returned, same as any other empty-retrieval case."""
    mock_user = MagicMock()
    mock_user.id = "u1"
    mock_user_service.get_user.side_effect = lambda uid: mock_user
    mock_user_service.add_trajectory_entry.side_effect = lambda *a: user_trajectory

    mock_retriever_result = MagicMock()
    mock_retriever_result.items = []
    mock_graphrag.retriever.search.side_effect = lambda *a, **kw: mock_retriever_result

    mock_user_service.get_user_trajectory_by_query_exact_match.side_effect = (
        lambda *a, **kw: []
    )
    mock_user_service.get_user_trajectory_by_query_similarity.side_effect = (
        lambda *a, **kw: []
    )
    mock_user_service.get_user_trajectory.side_effect = lambda *a, **kw: []

    svc = supervisor_agent_with_threshold
    svc._SupervisorAgentService__hint_agent.run_sync.return_value = MagicMock(output="")

    res = svc.retrieve_context("u1", "query", "c1")

    assert res is not None
    assert res.answer == SupervisorAgentService.RESPONSE_FALLBACK
