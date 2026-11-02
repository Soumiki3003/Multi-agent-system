from contextlib import AbstractContextManager
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
import logging
from typing import Any, Callable, Literal

from neo4j import ManagedTransaction, Session, unit_of_work
from neo4j_graphrag.generation import GraphRAG
from pydantic_ai import Embedder

from app import models, schemas
from app.utils import hash_string

from .auth import AuthService


class UserService:
    __user_node_name = "User"

    __trajectory_node_name = "UserTrajectory"
    __trajectory_rel_name = "HAS_TRAJECTORY"
    __trajectory_prev_rel_name = "PREVIOUS_TRAJECTORY"

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]],
        embedder: Embedder,
        rag: GraphRAG,
        auth_service: AuthService,
        trajectory_vector_index_field: str,
        trajectory_full_text_index_field: str,
    ):
        self.__session_factory = session_factory
        self.__embedder = embedder
        self.__auth_service = auth_service
        self.__rag = rag
        self.__trajectory_vector_index_field = trajectory_vector_index_field
        self.__trajectory_full_text_index_field = trajectory_full_text_index_field
        self.__logger = logging.getLogger(__name__)

    def create_user(self, item: schemas.CreateUser) -> models.User:
        @unit_of_work()
        def tx_fn(tx: ManagedTransaction, user: models.User) -> models.User:
            query = f"CREATE (u:{self.__user_node_name} $props) RETURN u"

            self.__logger.debug(f"Executing user creation query for {user.email}")
            result = tx.run(
                query,
                props=user.model_dump(mode="json", by_alias=True),
            )
            node = result.single(strict=True)
            self.__logger.debug("User node created in Neo4j")
            return models.User(**node["u"])

        self.__logger.info(f"Creating new user with email: {item.email}")
        self.__logger.debug(f"Checking if user with email {item.email} already exists")
        user_by_email = self.get_user_by_email(item.email)
        if user_by_email:
            self.__logger.warning(
                f"User creation failed: email {item.email} already exists"
            )
            raise ValueError(f"User with email {item.email} already exists")

        self.__logger.debug("Hashing user password")
        password_hash = self.__auth_service.hash_password(
            item.password.get_secret_value()
        )

        # Create User model with auto-generated ID
        new_user = models.User(
            name=item.name,
            email=item.email,
            password=password_hash,
            role=item.role,
            enabled=item.enabled,
        )

        with self.__session_factory() as session:
            new_user = session.execute_write(tx_fn, new_user)
        self.__logger.info(f"User created successfully: {item.email}")
        return new_user

    def get_user(self, id: str) -> models.User | None:
        @unit_of_work()
        def tx_fn(tx: ManagedTransaction, id: str) -> models.User | None:
            query = f"MATCH (u:{self.__user_node_name}) WHERE u.id = $id RETURN u"
            result = tx.run(query, id=id)
            record = result.single()
            if record:
                return models.User(**record["u"])
            return None

        with self.__session_factory() as session:
            user = session.execute_read(tx_fn, id)
        return user

    def get_user_by_email(self, email: str) -> models.User | None:
        @unit_of_work()
        def tx_fn(tx: ManagedTransaction, email: str) -> models.User | None:
            query = f"MATCH (u:{self.__user_node_name}) WHERE u.email = $email RETURN u"
            result = tx.run(query, email=email)
            record = result.single()
            if record:
                return models.User(**record["u"])
            return None

        with self.__session_factory() as session:
            user = session.execute_read(tx_fn, email)
        return user

    def authenticate(self, email: str, password: str) -> models.User | None:
        self.__logger.debug(f"Authenticating user: {email}")
        user = self.get_user_by_email(email)
        if not user:
            self.__logger.debug(f"User not found: {email}")
            return None
        if not self.__auth_service.verify_password(password, user.password):
            self.__logger.debug(f"Invalid password for user: {email}")
            return None
        self.__logger.info(f"User authenticated successfully: {email}")
        return user

    def update_user(
        self,
        id: str,
        *,
        to_update: schemas.UpdateUser,
    ) -> models.User:
        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction, id: str, to_update: schemas.UpdateUser
        ) -> models.User:
            params = to_update.model_dump(
                mode="json", exclude_unset=True, by_alias=True
            )
            query = f"""
            MATCH (u:{self.__user_node_name}) WHERE u.id = $id
            SET u += $props
            RETURN u
            """
            result = tx.run(query, id=id, props=params)
            node = result.single(strict=True)
            return models.User(**node["u"])

        with self.__session_factory() as session:
            return session.execute_write(tx_fn, id, to_update)

    def get_user_trajectory(
        self,
        user_id: str,
        *,
        course_id: str | None = None,
        challenge_stage: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
        timestamp_order: Literal["ASC", "DESC"] = "DESC",
    ) -> list[models.UserTrajectory]:
        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction,
            user_id: str,
            course_id: str | None,
            challenge_stage: int | None,
            *,
            limit: int | None = None,
            offset: int | None = None,
            timestamp_order: Literal["ASC", "DESC"] = "DESC",
        ) -> list[models.UserTrajectory]:
            if limit is not None and limit <= 0:
                raise ValueError("Limit must be a positive integer")
            if offset is not None and offset < 0:
                raise ValueError("Offset must be a non-negative integer")

            query = f"""
            MATCH (u:{self.__user_node_name})-[:{self.__trajectory_rel_name}]->(t:{self.__trajectory_node_name})
            WHERE u.id = $user_id
            """
            params: dict[str, Any] = {}
            if course_id is not None:
                query += " AND t.course_id = $course_id"
                params["course_id"] = course_id
            if challenge_stage is not None:
                query += " AND t.challenge_stage = $challenge_stage"
                params["challenge_stage"] = challenge_stage
            if limit is not None:
                query += " LIMIT $limit"
                params["limit"] = limit
            if offset is not None:
                query += " SKIP $offset"
                params["offset"] = offset
            query += (
                f" RETURN t, u.id AS user_id ORDER BY t.timestamp {timestamp_order}"
            )

            result = tx.run(
                query,
                params,
                user_id=user_id,
            )
            trajectories = []
            for record in result:
                data = dict(record["t"])
                data["user_id"] = record["user_id"]
                trajectories.append(models.UserTrajectory(**data))
            return trajectories

        with self.__session_factory() as session:
            return session.execute_read(
                tx_fn,
                user_id,
                course_id,
                challenge_stage,
                limit=limit,
                offset=offset,
                timestamp_order=timestamp_order,
            )

    def get_user_trajectory_by_query_exact_match(
        self,
        user_id: str,
        query: str,
        *,
        course_id: str | None = None,
        challenge_stage: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[models.UserTrajectory]:
        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction,
            user_id: str,
            query: str,
            course_id: str | None,
            challenge_stage: int | None,
            limit: int | None = None,
            offset: int | None = None,
        ) -> list[models.UserTrajectory]:

            statement = f"""
            MATCH (u:{self.__user_node_name})-[:{self.__trajectory_rel_name}]->(t:{self.__trajectory_node_name})
            WHERE u.id = $user_id AND t.{self.__trajectory_full_text_index_field} = $query_hash
            """

            parameters = {}
            if course_id is not None:
                statement += " AND t.course_id = $course_id"
                parameters["course_id"] = course_id
            if challenge_stage is not None:
                statement += " AND t.challenge_stage = $challenge_stage"
                parameters["challenge_stage"] = challenge_stage
            if limit is not None:
                statement += " LIMIT $limit"
                parameters["limit"] = limit
            if offset is not None:
                statement += " SKIP $offset"
                parameters["offset"] = offset
            statement += " RETURN t, u.id AS user_id ORDER BY t.timestamp DESC"

            result = tx.run(
                statement,
                parameters,
                user_id=user_id,
                query_hash=hash_string(query),
            )
            trajectories = []
            for record in result:
                data = dict(record["t"])
                data["user_id"] = record["user_id"]
                trajectories.append(models.UserTrajectory(**data))
            return trajectories

        with self.__session_factory() as session:
            return session.execute_read(
                tx_fn, user_id, query, course_id, challenge_stage, limit=limit, offset=offset
            )

    def get_user_trajectory_by_query_similarity(
        self,
        user_id: str,
        query: str,
        *,
        course_id: str | None = None,
        challenge_stage: int | None = None,
        threshold: float | None = None,
        limit: int | None = None,
    ) -> list[models.UserTrajectory]:
        top_k = limit or 20
        filters: dict[str, Any] = {"user_id": user_id}
        if course_id is not None:
            filters["course_id"] = course_id
        if challenge_stage is not None:
            filters["challenge_stage"] = challenge_stage
        try:
            result = self.__rag.retriever.get_search_results(
                query_text=query,
                filters=filters,
                top_k=top_k,
            )
            trajectories = []
            for record in result.records:
                score = record.get("score") or 0.0
                if threshold is not None and score < threshold:
                    continue
                node = record.get("node")
                if node is None:
                    continue
                try:
                    data = dict(node)
                    data["user_id"] = data.get("user_id") or user_id
                    trajectories.append(models.UserTrajectory(**data))
                except Exception as parse_err:
                    self.__logger.debug(f"Skipping malformed trajectory record: {parse_err}")
            return trajectories
        except Exception as e:
            self.__logger.warning(
                f"Vector retriever search failed during similarity retrieval: {e}"
            )
            return []

    def add_trajectory_entry(
        self,
        user_id: str,
        trajectory_entry: models.UserTrajectory,
    ) -> models.UserTrajectory:
        self.__logger.info(f"Adding trajectory entry for user {user_id}")
        self.__logger.debug(f"Query: {trajectory_entry.query[:100]}...")

        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction,
            user_id: str,
            trajectory_entry: models.UserTrajectory,
        ) -> models.UserTrajectory:
            query = f"""
            MATCH (u:{self.__user_node_name}) WHERE u.id = $user_id
            CREATE (t:{self.__trajectory_node_name} $props)
            CREATE (u)-[rel:{self.__trajectory_rel_name}]->(t)
            WITH u, t, rel
            OPTIONAL MATCH (u)-[prev_rel:{self.__trajectory_rel_name}]->(prev_t:{self.__trajectory_node_name})
            WHERE prev_t <> t
            ORDER BY prev_t.timestamp DESC
            LIMIT 1
            FOREACH (_ IN CASE WHEN prev_t IS NOT NULL THEN [1] ELSE [] END |
                CREATE (t)-[:{self.__trajectory_prev_rel_name}]->(prev_t)
            )
            RETURN t, u.id AS user_id
            """

            params = trajectory_entry.model_dump(mode="json", by_alias=True)
            self.__logger.debug("Hashing query for full-text index")
            params[self.__trajectory_full_text_index_field] = hash_string(
                trajectory_entry.query
            )
            self.__logger.debug("Embedding query for vector search")
            try:
                params[self.__trajectory_vector_index_field] = (
                    self.__embedder.embed_documents_sync([trajectory_entry.query])[0]
                )
            except Exception as embed_err:
                self.__logger.warning(
                    f"Embedding failed, storing zero vector: {embed_err}"
                )
                params[self.__trajectory_vector_index_field] = [0.0] * 384

            self.__logger.debug("Creating trajectory node in Neo4j")
            result = tx.run(
                query,
                props=params,
                user_id=user_id,
            )
            node = result.single(strict=True)
            data = dict(node["t"])
            data["user_id"] = node["user_id"]
            self.__logger.debug("Trajectory node created successfully")
            return models.UserTrajectory(**data)

        with self.__session_factory() as session:
            result = session.execute_write(tx_fn, user_id, trajectory_entry)
        self.__logger.info(f"Trajectory entry added successfully for user {user_id}")
        return result

    def increment_trajectory_query_repeat_count(
        self, trajectory_id: str, *, increment: int = 1
    ) -> models.UserTrajectory:
        self.__logger.debug(
            f"Incrementing query repeat count for trajectory {trajectory_id} by {increment}"
        )

        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction,
            trajectory_id: str,
            increment: int,
        ) -> models.UserTrajectory:
            query = f"""
            MATCH (t:{self.__trajectory_node_name}) WHERE id(t) = $trajectory_id
            SET t.query_repeat_count = t.query_repeat_count + $increment
            RETURN t
            """
            result = tx.run(
                query,
                trajectory_id=trajectory_id,
                increment=increment,
            )
            node = result.single(strict=True)
            return models.UserTrajectory(**node["t"])

        with self.__session_factory() as session:
            return session.execute_write(tx_fn, trajectory_id, increment)

    def get_users_by_role(self, role: str) -> list[models.User]:
        @unit_of_work()
        def tx_fn(tx: ManagedTransaction, role: str) -> list[models.User]:
            query = (
                f"MATCH (u:{self.__user_node_name}) "
                "WHERE u.role = $role AND u.enabled = true "
                "RETURN u ORDER BY u.name"
            )
            result = tx.run(query, role=role)
            return [models.User(**record["u"]) for record in result]

        with self.__session_factory() as session:
            return session.execute_read(tx_fn, role)

    def get_pending_hints(
        self, course_id: str
    ) -> list[tuple[models.UserTrajectory, str]]:
        """Return (trajectory, user_name) tuples for all pending hints in a course."""

        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction, course_id: str
        ) -> list[tuple[models.UserTrajectory, str]]:
            query = f"""
            MATCH (u:{self.__user_node_name})-[:{self.__trajectory_rel_name}]->(t:{self.__trajectory_node_name})
            WHERE t.course_id = $course_id
              AND t.hint_triggered = true
              AND t.hint_approval_status = $approval_status
            RETURN t, u.id AS user_id, u.name AS user_name
            ORDER BY t.timestamp DESC
            """
            result = tx.run(
                query,
                course_id=course_id,
                approval_status=models.HintApprovalStatus.PENDING,
            )
            items = []
            for record in result:
                data = dict(record["t"])
                data["user_id"] = record["user_id"]
                trajectory = models.UserTrajectory(**data)
                items.append((trajectory, record["user_name"]))
            return items

        with self.__session_factory() as session:
            return session.execute_read(tx_fn, course_id)

    def update_hint_approval(
        self,
        trajectory_id: str,
        status: models.HintApprovalStatus,
        *,
        hint_text: str | None = None,
    ) -> models.UserTrajectory | None:
        """Set the approval status (and optionally the hint text) on a trajectory node.

        ``hint_text`` is only written when the caller supplies a non-empty value,
        so that passing ``None`` leaves the existing text untouched.
        """

        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction,
            trajectory_id: str,
            status: str,
            hint_text: str | None,
        ) -> models.UserTrajectory | None:
            set_clause = "SET t.hint_approval_status = $status"
            params: dict = {"trajectory_id": trajectory_id, "status": status}
            if hint_text is not None:
                set_clause += ", t.hint_text = $hint_text"
                params["hint_text"] = hint_text

            query = f"""
            MATCH (u:{self.__user_node_name})-[:{self.__trajectory_rel_name}]->(t:{self.__trajectory_node_name})
            WHERE t.id = $trajectory_id
            {set_clause}
            RETURN t, u.id AS user_id
            """
            result = tx.run(query, **params)
            record = result.single()
            if not record:
                return None
            data = dict(record["t"])
            data["user_id"] = record["user_id"]
            return models.UserTrajectory(**data)

        with self.__session_factory() as session:
            return session.execute_write(tx_fn, trajectory_id, str(status), hint_text)

    def get_approved_hints_for_student(
        self, user_id: str, course_id: str, challenge_stage: int | None = None
    ) -> list[models.UserTrajectory]:
        """Return approved hints for a student that have not yet expired.

        A hint expires 24 h after it was first read (``hint_read_when`` is set on
        first view).  Hints that have never been read are always returned.
        When ``challenge_stage`` is given, only hints for that stage are returned.
        """
        expiry_threshold = (
            datetime.now(timezone.utc) - relativedelta(hours=24)
        ).isoformat()

        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction, user_id: str, course_id: str, expiry_threshold: str,
            challenge_stage: int | None,
        ) -> list[models.UserTrajectory]:
            stage_filter = "AND t.challenge_stage = $challenge_stage" if challenge_stage is not None else ""
            params = {
                "user_id": user_id,
                "course_id": course_id,
                "approval_status": models.HintApprovalStatus.APPROVED,
                "expiry_threshold": expiry_threshold,
            }
            if challenge_stage is not None:
                params["challenge_stage"] = challenge_stage
            query = f"""
            MATCH (u:{self.__user_node_name})-[:{self.__trajectory_rel_name}]->(t:{self.__trajectory_node_name})
            WHERE u.id = $user_id
              AND t.course_id = $course_id
              AND t.hint_triggered = true
              AND t.hint_approval_status = $approval_status
              AND (t.hint_read_when IS NULL OR t.hint_read_when > $expiry_threshold)
              {stage_filter}
            RETURN t, u.id AS user_id
            ORDER BY t.timestamp ASC
            """
            result = tx.run(query, **params)
            items = []
            for record in result:
                data = dict(record["t"])
                data["user_id"] = record["user_id"]
                items.append(models.UserTrajectory(**data))
            return items

        with self.__session_factory() as session:
            return session.execute_read(tx_fn, user_id, course_id, expiry_threshold, challenge_stage)

    def get_all_course_hints_for_student(
        self, user_id: str, course_id: str, challenge_stage: int | None = None
    ) -> list[models.UserTrajectory]:
        """Return all approved hints for a student in a course (no expiry filter).

        Used by the survey so students can review every hint they received
        regardless of when it was read. When ``challenge_stage`` is given, only
        hints from that specific challenge are returned — this keeps a CTF's
        hints scoped to that CTF's own post-challenge survey instead of
        bleeding into every other survey (pre-study, other challenges, etc.).
        """
        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction, user_id: str, course_id: str, challenge_stage: int | None,
        ) -> list[models.UserTrajectory]:
            stage_filter = "AND t.challenge_stage = $challenge_stage" if challenge_stage is not None else ""
            params = {
                "user_id": user_id,
                "course_id": course_id,
                "approval_status": models.HintApprovalStatus.APPROVED,
            }
            if challenge_stage is not None:
                params["challenge_stage"] = challenge_stage
            query = f"""
            MATCH (u:{self.__user_node_name})-[:{self.__trajectory_rel_name}]->(t:{self.__trajectory_node_name})
            WHERE u.id = $user_id
              AND t.course_id = $course_id
              AND t.hint_triggered = true
              AND t.hint_approval_status = $approval_status
              {stage_filter}
            RETURN t, u.id AS user_id
            ORDER BY t.timestamp ASC
            """
            result = tx.run(query, **params)
            items = []
            for record in result:
                data = dict(record["t"])
                data["user_id"] = record["user_id"]
                items.append(models.UserTrajectory(**data))
            return items

        with self.__session_factory() as session:
            return session.execute_read(tx_fn, user_id, course_id, challenge_stage)

    def mark_hint_read(self, trajectory_id: str, course_id: str) -> None:
        """Record the first time a student sees a hint (sets ``hint_read_when``).

        ``course_id`` is matched to ensure the trajectory belongs to the
        expected course before updating. If the hint was already read the
        timestamp is left unchanged so the 24-hour expiry window is not reset
        by subsequent views.
        """

        @unit_of_work()
        def tx_fn(
            tx: ManagedTransaction,
            trajectory_id: str,
            course_id: str,
            hint_read_when: str,
        ) -> None:
            query = f"""
            MATCH (t:{self.__trajectory_node_name})
            WHERE t.id = $trajectory_id
              AND t.course_id = $course_id
              AND t.hint_read_when IS NULL
            SET t.hint_read_when = $hint_read_when
            """
            tx.run(
                query,
                trajectory_id=trajectory_id,
                course_id=course_id,
                hint_read_when=hint_read_when,
            )

        hint_read_when = datetime.now(timezone.utc).isoformat()
        with self.__session_factory() as session:
            session.execute_write(tx_fn, trajectory_id, course_id, hint_read_when)

    def record_survey_completion(self, user_id: str, course_id: str, survey_id: str) -> None:
        @unit_of_work()
        def tx_fn(tx: ManagedTransaction, user_id: str, course_id: str, survey_id: str) -> None:
            query = f"""
            MATCH (u:{self.__user_node_name}) WHERE u.id = $user_id
            MERGE (u)-[:COMPLETED_SURVEY]->(sc:SurveyCompletion {{user_id: $user_id, course_id: $course_id, survey_id: $survey_id}})
            ON CREATE SET sc.id = $sc_id, sc.completed_at = $completed_at
            """
            import uuid
            from datetime import datetime, timezone
            tx.run(
                query,
                user_id=user_id,
                course_id=course_id,
                survey_id=survey_id,
                sc_id=uuid.uuid4().hex,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )

        with self.__session_factory() as session:
            session.execute_write(tx_fn, user_id, course_id, survey_id)

    def get_completed_survey_ids(self, user_id: str, course_id: str) -> list[str]:
        @unit_of_work()
        def tx_fn(tx: ManagedTransaction, user_id: str, course_id: str) -> list[str]:
            query = f"""
            MATCH (u:{self.__user_node_name})-[:COMPLETED_SURVEY]->(sc:SurveyCompletion)
            WHERE u.id = $user_id AND sc.course_id = $course_id
            RETURN sc.survey_id AS survey_id
            """
            result = tx.run(query, user_id=user_id, course_id=course_id)
            return [record["survey_id"] for record in result]

        with self.__session_factory() as session:
            return session.execute_read(tx_fn, user_id, course_id)

    def delete_user(self, id: str) -> None:
        to_update = schemas.UpdateUser(enabled=False)
        self.update_user(id, to_update=to_update)
