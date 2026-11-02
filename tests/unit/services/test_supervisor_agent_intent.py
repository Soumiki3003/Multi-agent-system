import pytest
from unittest.mock import MagicMock
from app.services.supervisor_agent import SupervisorAgentService


@pytest.fixture
def supervisor_agent(mock_user_service, mock_graphrag):
    return SupervisorAgentService(
        user_service=mock_user_service,
        graph_rag=mock_graphrag,
        hint_agent=MagicMock(),
    )


class TestClassifyIntent:
    """Test __classify_intent method for correct intent classification."""

    def test_definition_what_is(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "what is symbolic execution"
        )
        assert intent == "definition"

    def test_definition_explain(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "explain how dependency injection works"
        )
        assert intent == "definition"

    def test_definition_define(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "define polymorphism"
        )
        assert intent == "definition"

    def test_definition_what_are(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "what are the principles of OOP"
        )
        assert intent == "definition"

    def test_definition_meaning_of(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "meaning of encapsulation"
        )
        assert intent == "definition"

    def test_definition_describe(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "describe the MVC pattern"
        )
        assert intent == "definition"

    def test_procedural_how_to(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "how to run the tests"
        )
        assert intent == "procedural"

    def test_procedural_how_do(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "how do I implement a binary search"
        )
        assert intent == "procedural"

    def test_procedural_steps_to(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "steps to deploy the application"
        )
        assert intent == "procedural"

    def test_procedural_implement(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "implement a linked list"
        )
        assert intent == "procedural"

    def test_procedural_how_can_i(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "how can i debug this code"
        )
        assert intent == "procedural"

    def test_troubleshooting_error_in(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "error in my sorting algorithm"
        )
        assert intent == "troubleshooting"

    def test_troubleshooting_not_working(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "my function is not working"
        )
        assert intent == "troubleshooting"

    def test_troubleshooting_fix(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "fix the memory leak"
        )
        assert intent == "troubleshooting"

    def test_troubleshooting_bug(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "there is a bug in the parser"
        )
        assert intent == "troubleshooting"

    def test_troubleshooting_exception(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "getting an exception when running"
        )
        assert intent == "troubleshooting"

    def test_example_request_example_of(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "example of recursion"
        )
        assert intent == "example_request"

    def test_example_request_show_me(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "show me an example of recursion"
        )
        assert intent == "example_request"

    def test_example_request_sample(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "give me a sample test"
        )
        assert intent == "example_request"

    def test_ambiguous_query_defaults_to_context_request(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "hello there"
        )
        assert intent == "context_request"

    def test_empty_query_defaults_to_context_request(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent("")
        assert intent == "context_request"

    def test_whitespace_query_defaults_to_context_request(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent("   ")
        assert intent == "context_request"

    def test_vague_query_defaults_to_context_request(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "tell me about graphs"
        )
        assert intent == "context_request"

    def test_case_insensitive(self, supervisor_agent):
        intent = supervisor_agent._SupervisorAgentService__classify_intent(
            "WHAT IS the answer"
        )
        assert intent == "definition"


def _setup_trajectory_mocks(
    mock_user_service, mock_graphrag, supervisor_agent, user_trajectory
):
    """Common setup for trajectory integration tests."""
    mock_user = MagicMock()
    mock_user.id = "u1"
    mock_user_service.get_user.side_effect = lambda uid: mock_user

    captured_traj = {}

    def capture_add(user_id, traj):
        captured_traj["traj"] = traj
        return user_trajectory

    mock_user_service.add_trajectory_entry.side_effect = capture_add

    item = MagicMock()
    item.content = "{'name': 'TestNode'}"
    item.metadata = {"score": 0.9}
    mock_retriever_result = MagicMock()
    mock_retriever_result.items = [item]
    mock_graphrag.retriever.search.side_effect = lambda *a, **kw: mock_retriever_result

    mock_user_service.get_user_trajectory_by_query_exact_match.side_effect = (
        lambda *a, **kw: []
    )
    mock_user_service.get_user_trajectory_by_query_similarity.side_effect = (
        lambda *a, **kw: []
    )

    mock_hint_out = MagicMock()
    mock_hint_out.output = ""
    supervisor_agent._SupervisorAgentService__hint_agent.run_sync.return_value = (
        mock_hint_out
    )
    mock_user_service.get_user_trajectory.side_effect = lambda *a, **kw: []

    return captured_traj


class TestIntentStoredInTrajectory:
    """Test that classified intent is stored in the trajectory entry."""

    def test_definition_intent_stored(
        self, supervisor_agent, mock_user_service, mock_graphrag, user_trajectory
    ):
        captured = _setup_trajectory_mocks(
            mock_user_service, mock_graphrag, supervisor_agent, user_trajectory
        )
        supervisor_agent.retrieve_context(
            "u1", "what is symbolic execution", "c1"
        )
        assert captured["traj"].interaction_type == "definition"

    def test_procedural_intent_stored(
        self, supervisor_agent, mock_user_service, mock_graphrag, user_trajectory
    ):
        captured = _setup_trajectory_mocks(
            mock_user_service, mock_graphrag, supervisor_agent, user_trajectory
        )
        supervisor_agent.retrieve_context(
            "u1", "how to deploy the application", "c1"
        )
        assert captured["traj"].interaction_type == "procedural"

    def test_troubleshooting_intent_stored(
        self, supervisor_agent, mock_user_service, mock_graphrag, user_trajectory
    ):
        captured = _setup_trajectory_mocks(
            mock_user_service, mock_graphrag, supervisor_agent, user_trajectory
        )
        supervisor_agent.retrieve_context(
            "u1", "bug in my sorting logic", "c1"
        )
        assert captured["traj"].interaction_type == "troubleshooting"

    def test_example_request_intent_stored(
        self, supervisor_agent, mock_user_service, mock_graphrag, user_trajectory
    ):
        captured = _setup_trajectory_mocks(
            mock_user_service, mock_graphrag, supervisor_agent, user_trajectory
        )
        supervisor_agent.retrieve_context(
            "u1", "show me an example of recursion", "c1"
        )
        assert captured["traj"].interaction_type == "example_request"

    def test_context_request_stored_for_ambiguous(
        self, supervisor_agent, mock_user_service, mock_graphrag, user_trajectory
    ):
        captured = _setup_trajectory_mocks(
            mock_user_service, mock_graphrag, supervisor_agent, user_trajectory
        )
        supervisor_agent.retrieve_context(
            "u1", "tell me about graphs", "c1"
        )
        assert captured["traj"].interaction_type == "context_request"
