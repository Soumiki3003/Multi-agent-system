import re
from dependency_injector.wiring import Provide, inject
from flask import Blueprint, Response, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_pydantic import validate

from app import controllers, schemas, services
from app.challenges import UNLOCK_SEQUENCE, POST_STUDY_STAGE, build_breadcrumb
from app.schemas.knowledge import ALLOWED_CHILDREN, BLOOM_LEVELS
from app.containers import Application
from app.services.chat_export import build_chat_transcript_pdf
from app.views.guards import roles_required

app = Blueprint("course", __name__)


@app.route("/", methods=["GET"])
@login_required
@validate()
@inject
def dashboard(
    form: schemas.Paginated,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
    allowed_extensions: list[str] = Provide[Application.core.allowed_extensions],
):
    paginated_courses = schemas.PaginatedCourses(
        page=form.page,
        page_size=form.page_size,
        user_id=current_user.id,
        user_role=current_user.role.value,
    )
    courses, has_next = course_controller.get_courses(paginated_courses)
    all_instructors = course_controller.get_users_by_role("instructor")
    all_students = course_controller.get_users_by_role("student")

    return render_template(
        "course/dashboard.html",
        courses=courses,
        page=form.page,
        page_size=form.page_size,
        has_next=has_next,
        allowed_extensions=allowed_extensions,
        all_instructors=all_instructors,
        all_students=all_students,
    )


@app.route("/course/create", methods=["POST"])
@roles_required("instructor")
@validate()
@inject
def create_course(
    body: schemas.CreateCourse,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    course_id = course_controller.create_course(body, creator_id=current_user.id)
    return {"id": course_id, "redirect": "/"}, 201


@app.route("/course/<course_id>/chat", methods=["GET"])
@login_required
@inject
def chat(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    if current_user.role == "student":
        current_stage = course_controller.get_current_stage(current_user.id, course_id)
        if current_stage == 0:
            return redirect(url_for("course.pre_study", course_id=course_id))
        if current_stage >= POST_STUDY_STAGE:
            return redirect(url_for("course.post_study", course_id=course_id))
        return redirect(url_for("course.challenge_chat", course_id=course_id, stage=current_stage))

    course = course_controller.get_course(course_id)
    messages = course_controller.get_chat_history(current_user.id, course_id)
    return render_template(
        "course/chat.html",
        course=course,
        course_id=course_id,
        messages=messages,
    )


@app.route("/course/<course_id>/chat/send", methods=["POST"])
@login_required
@validate()
@inject
def chat_send(
    course_id: str,
    form: schemas.ChatUserMessageFormRequest,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    try:
        result = course_controller.chat_send(current_user.id, course_id, form.content)
    except Exception:
        result = schemas.ChatResponse(
            answer="An error occurred while processing your message."
        )

    return render_template(
        "course/chat_message.html",
        user_message=form.content,
        assistant_message=result.answer,
        hint_text=result.hint_text,
        user_name=current_user.name,
    )


@app.route("/course/<course_id>/pre-study", methods=["GET"])
@login_required
@inject
def pre_study(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    course = course_controller.get_course(course_id)
    current_stage = course_controller.get_current_stage(current_user.id, course_id)
    breadcrumb = build_breadcrumb(course_id, current_stage, viewed_stage=0)
    return render_template(
        "course/pre_study.html",
        course=course,
        course_id=course_id,
        breadcrumb=breadcrumb,
        current_stage=current_stage,
    )


@app.route("/course/<course_id>/post-study", methods=["GET"])
@login_required
@inject
def post_study(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    course = course_controller.get_course(course_id)
    current_stage = course_controller.get_current_stage(current_user.id, course_id)
    if current_stage < POST_STUDY_STAGE:
        return redirect(url_for("course.chat", course_id=course_id))
    breadcrumb = build_breadcrumb(course_id, current_stage, viewed_stage=POST_STUDY_STAGE)
    is_completed = current_stage > POST_STUDY_STAGE
    return render_template(
        "course/post_study.html",
        course=course,
        course_id=course_id,
        breadcrumb=breadcrumb,
        current_stage=current_stage,
        is_completed=is_completed,
    )


@app.route("/course/<course_id>/challenge/<int:stage>", methods=["GET"])
@login_required
@inject
def challenge_chat(
    course_id: str,
    stage: int,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    current_stage = course_controller.get_current_stage(current_user.id, course_id)

    # post_study stage is not a challenge chat page
    if stage >= POST_STUDY_STAGE:
        return redirect(url_for("course.post_study", course_id=course_id))
    if stage > current_stage:
        return redirect(url_for("course.pre_study", course_id=course_id))

    course = course_controller.get_course(course_id)
    messages = course_controller.get_challenge_history(current_user.id, course_id, stage)
    breadcrumb = build_breadcrumb(course_id, current_stage, viewed_stage=stage)
    is_active = stage == current_stage
    survey_id = UNLOCK_SEQUENCE[stage] if stage < len(UNLOCK_SEQUENCE) else None

    return render_template(
        "course/chat.html",
        course=course,
        course_id=course_id,
        messages=messages,
        breadcrumb=breadcrumb,
        stage=stage,
        current_stage=current_stage,
        is_active=is_active,
        survey_id=survey_id,
    )


@app.route("/course/<course_id>/challenge/<int:stage>/send", methods=["POST"])
@login_required
@validate()
@inject
def challenge_chat_send(
    course_id: str,
    stage: int,
    form: schemas.ChatUserMessageFormRequest,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    try:
        result = course_controller.chat_send(current_user.id, course_id, form.content, stage=stage)
    except Exception:
        result = schemas.ChatResponse(
            answer="An error occurred while processing your message."
        )

    return render_template(
        "course/chat_message.html",
        user_message=form.content,
        assistant_message=result.answer,
        hint_text=result.hint_text,
        user_name=current_user.name,
    )


@app.route("/course/<course_id>/settings", methods=["GET"])
@roles_required("instructor")
@inject
def settings(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
    allowed_extensions: list[str] = Provide[Application.core.allowed_extensions],
):
    course = course_controller.get_course(course_id)
    uploads = course_controller.get_uploads()
    members = course_controller.get_course_members(course_id)
    all_instructors = course_controller.get_users_by_role("instructor")
    all_students = course_controller.get_users_by_role("student")
    return render_template(
        "course/settings.html",
        course=course,
        course_id=course_id,
        uploads=uploads,
        allowed_extensions=allowed_extensions,
        allowed_children=ALLOWED_CHILDREN,
        bloom_levels=BLOOM_LEVELS,
        members=members,
        all_instructors=all_instructors,
        all_students=all_students,
    )


@app.route("/course/<course_id>/members", methods=["PUT"])
@roles_required("instructor")
@validate()
@inject
def update_members(
    course_id: str,
    body: schemas.UpdateCourseMembers,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    try:
        course_controller.update_course_members(course_id, body)
        return {"success": True}, 200
    except ValueError as e:
        return {"error": str(e)}, 404
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/course/<course_id>/members", methods=["GET"])
@roles_required("instructor")
@inject
def get_members(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    members = course_controller.get_course_members(course_id)
    return jsonify(
        {
            "instructors": [
                m.model_dump(mode="json", by_alias=True) for m in members["instructors"]
            ],
            "students": [
                m.model_dump(mode="json", by_alias=True) for m in members["students"]
            ],
        }
    )


@app.route("/course/<course_id>/delete", methods=["DELETE"])
@roles_required("instructor")
@inject
def delete_course(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    try:
        course_controller.delete_course(course_id)
        return {"success": True, "redirect": "/"}, 200
    except ValueError as e:
        return {"error": str(e)}, 404
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/course/<course_id>/clear", methods=["DELETE"])
@roles_required("instructor")
@inject
def clear_course(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    try:
        course_controller.clear_course(course_id)
        return {"success": True}, 200
    except ValueError as e:
        return {"error": str(e)}, 404
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/course/<course_id>/hints", methods=["GET"])
@roles_required("instructor")
@inject
def get_hints(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Return the pending-hints partial for the instructor settings page."""
    hints = course_controller.get_pending_hints(course_id)
    return render_template(
        "course/hint_item.html",
        hints=hints,
        course_id=course_id,
    )


@app.route("/course/<course_id>/hints/manual", methods=["POST"])
@roles_required("instructor")
@validate()
@inject
def create_manual_hint(
    course_id: str,
    form: schemas.CreateManualHint,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Manually create a hint for a student or all students."""
    try:
        course_controller.create_manual_hint(course_id, form)
        return {"success": True}, 201
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/course/<course_id>/hints/<trajectory_id>", methods=["PUT"])
@roles_required("instructor")
@validate()
@inject
def update_hint(
    course_id: str,
    trajectory_id: str,
    form: schemas.UpdateHintApprovalRequest,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Approve or reject a hint; returns the updated single-hint card."""
    try:
        # Strip blank-only edits so an empty textarea doesn't overwrite the text
        hint_text = form.hint_text.strip() if form.hint_text else None
        trajectory = course_controller.update_hint_approval(
            trajectory_id, form.status, hint_text=hint_text or None
        )
        if not trajectory:
            return {"error": "Hint not found"}, 404
        return render_template(
            "course/hint_item.html",
            hints=[],  # empty list signals the row should be removed
            course_id=course_id,
        )
    except ValueError as e:
        return {"error": str(e)}, 400
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/course/<course_id>/hints/student/count", methods=["GET"])
@login_required
@inject
def student_hints_count(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Return the hint-badge count fragment (HTMX polling target)."""
    stage = request.args.get("stage", type=int)
    hints = course_controller.get_approved_hints(current_user.id, course_id, challenge_stage=stage)
    return render_template("course/hint_badge.html", count=len(hints))


@app.route("/course/<course_id>/hints/student", methods=["GET"])
@login_required
@inject
def student_hints(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Return a single approved hint at the requested index (HTMX modal body)."""
    index = request.args.get("index", 0, type=int)
    stage = request.args.get("stage", type=int)
    hints = course_controller.get_approved_hints(current_user.id, course_id, challenge_stage=stage)
    total = len(hints)
    hint = hints[index] if hints and 0 <= index < total else None
    return render_template(
        "course/approved_hints.html",
        hint=hint,
        index=index,
        total=total,
        course_id=course_id,
    )


@app.route("/course/<course_id>/hints/<trajectory_id>/read", methods=["POST"])
@login_required
@inject
def mark_hint_read(
    course_id: str,
    trajectory_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Mark a hint as read the first time the student views it."""
    course_controller.mark_hint_read(trajectory_id, course_id)
    return "", 204


@app.route("/course/<course_id>/upload", methods=["POST"])
@roles_required("instructor")
@inject
def upload_to_course(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    files = request.files.getlist("files")
    if not files:
        return {"error": "No files provided"}, 400

    try:
        processed = course_controller.upload_to_course(course_id, files)
        return {"success": True, "processed": processed}, 200
    except ValueError as e:
        return {"error": str(e)}, 400
    except Exception as e:
        return {"error": str(e)}, 500


# ── Dashboard Progress ──────────────────────────────────────────────


@app.route("/course/<course_id>/progress", methods=["GET"])
@roles_required("instructor")
@inject
def dashboard_progress(
    course_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    course = course_controller.get_course(course_id)
    return render_template(
        "course/dashboard_progress.html",
        course=course,
        course_id=course_id,
    )


@app.route("/course/<course_id>/api/student-chat/<student_id>", methods=["GET"])
@roles_required("instructor")
@inject
def api_student_chat(
    course_id: str,
    student_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Return a read-only chat transcript for a student's specific CTF stage."""
    stage = request.args.get("stage", 1, type=int)
    messages = course_controller.get_challenge_history(student_id, course_id, stage)
    return render_template(
        "course/_student_chat.html",
        messages=messages,
        stage=stage,
        student_id=student_id,
        course_id=course_id,
    )


@app.route("/course/<course_id>/api/student-chat/<student_id>/pdf", methods=["GET"])
@roles_required("instructor")
@inject
def api_student_chat_pdf(
    course_id: str,
    student_id: str,
    *,
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Download a student's chat transcript for a specific CTF stage as a PDF."""
    stage = request.args.get("stage", 1, type=int)
    messages = course_controller.get_challenge_history(student_id, course_id, stage)

    course = course_controller.get_course(course_id)
    try:
        members = course_controller.get_course_members(course_id)
        info_map = {s.id: {"name": s.name, "email": s.email} for s in members.get("students", [])}
        info = info_map.get(student_id, {})
        student_name = info.get("name", student_id)
        student_email = info.get("email", "")
    except Exception:
        student_name = student_id
        student_email = ""

    ctf_label = f"CTF-{stage - 1:02d}"
    pdf_bytes = build_chat_transcript_pdf(
        course.name, student_name, student_email, ctf_label, messages
    )

    filename = f"chat_{ctf_label}_{student_name.replace(' ', '_')}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/course/<course_id>/api/node-struggle", methods=["GET"])
@roles_required("instructor")
@inject
def api_node_struggle(
    course_id: str,
    *,
    dashboard_service: services.DashboardService = Provide[
        Application.services.dashboard
    ],
):
    """Return JSON array of {node_id, node_name, question_count, hint_count, student_count}."""
    return jsonify(dashboard_service.calculate_node_engagement(course_id))


@app.route("/course/<course_id>/api/student-struggle", methods=["GET"])
@roles_required("instructor")
@inject
def api_student_struggle(
    course_id: str,
    *,
    dashboard_service: services.DashboardService = Provide[
        Application.services.dashboard
    ],
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Return JSON array of {student_id, student_name, email, total_questions, hints_triggered, unique_topics}."""
    activity = dashboard_service.calculate_student_activity(course_id)
    try:
        members = course_controller.get_course_members(course_id)
        info_map = {s.id: {"name": s.name, "email": s.email} for s in members.get("students", [])}
    except Exception:
        info_map = {}

    enriched = []
    for entry in activity:
        info = info_map.get(entry["student_id"], {})
        enriched.append({
            "student_id": entry["student_id"],
            "student_name": info.get("name", entry["student_id"]),
            "student_email": info.get("email", ""),
            "total_questions": entry["total_questions"],
            "hints_triggered": entry["hints_triggered"],
            "unique_topics": entry["unique_topics"],
            "questions": entry["questions"],
        })
    return jsonify(enriched)


@app.route("/course/<course_id>/api/struggle-detail/node/<node_id>", methods=["GET"])
@roles_required("instructor")
@inject
def api_node_struggle_detail(
    course_id: str,
    node_id: str,
    *,
    dashboard_service: services.DashboardService = Provide[
        Application.services.dashboard
    ],
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Return HTMX partial: rich breakdown for a conceptual topic node."""
    trajectories = dashboard_service._fetch_trajectories(course_id)
    conceptual_nodes = dashboard_service._fetch_conceptual_nodes(course_id)

    # Look up label for this node_id
    node_label = node_id
    for cn in conceptual_nodes:
        if cn.get("name") == node_id:
            node_label = cn.get("label") or node_id
            break

    try:
        members = course_controller.get_course_members(course_id)
        name_map = {s.id: {"name": s.name, "email": getattr(s, "email", "")} for s in members.get("students", [])}
    except Exception:
        name_map = {}

    from collections import defaultdict

    student_data: dict[str, dict] = defaultdict(lambda: {
        "queries": [], "hints": 0, "procedural_queries": []
    })
    total_questions = 0
    total_hints = 0

    for entry in trajectories:
        matched = dashboard_service._match_conceptual_node(entry.query, conceptual_nodes, entry.retrieved_nodes)
        if not matched or matched.get("name") != node_id:
            continue
        total_questions += 1
        if entry.hint_triggered:
            total_hints += 1
        uid = entry.user_id
        q_entry = {
            "query": entry.query,
            "interaction_type": entry.interaction_type,
            "hint_triggered": entry.hint_triggered,
        }
        student_data[uid]["queries"].append(q_entry)
        if entry.hint_triggered:
            student_data[uid]["hints"] += 1
        # Detect procedural queries via: interaction type, retrieved node names,
        # or procedural keywords in the query text itself
        _procedural_kws = {
            "how", "step", "steps", "solve", "run", "execute", "implement",
            "do i", "do you", "write", "code", "script", "function",
            "procedure", "initialize", "setup", "load", "create", "use",
            "install", "configure", "build", "start", "launch", "call", "fill", "???",
        }
        q_lower = entry.query.lower()
        has_procedural = (
            entry.interaction_type == "code_request"
            or any(kw in q_lower for kw in _procedural_kws)
            or any(
                re.search(r"(^|[-_])step\d*$|^[Pp]\d+", rn)
                for rn in (entry.retrieved_nodes or [])
            )
        )
        if has_procedural:
            # Extract procedural node names from retrieved_nodes
            proc_nodes = [
                rn for rn in (entry.retrieved_nodes or [])
                if re.search(r"(^|[-_])step\d*|^[Pp]\d+|procedural", rn, re.IGNORECASE)
            ]
            student_data[uid]["procedural_queries"].append({
                "query": entry.query,
                "nodes": proc_nodes,
            })

    students = []
    for uid, data in student_data.items():
        info = name_map.get(uid, {})
        students.append({
            "student_id": uid,
            "student_name": info.get("name", uid),
            "student_email": info.get("email", ""),
            "query_count": len(data["queries"]),
            "hints": data["hints"],
            "procedural_queries": data["procedural_queries"],
            "queries": data["queries"],
        })
    students.sort(key=lambda s: s["query_count"], reverse=True)

    return render_template(
        "course/_struggle_detail.html",
        detail_type="node",
        node_id=node_id,
        node_label=node_label,
        total_questions=total_questions,
        total_hints=total_hints,
        students=students,
        course_id=course_id,
    )


@app.route(
    "/course/<course_id>/api/struggle-detail/student/<student_id>", methods=["GET"]
)
@roles_required("instructor")
@inject
def api_student_struggle_detail(
    course_id: str,
    student_id: str,
    *,
    dashboard_service: services.DashboardService = Provide[
        Application.services.dashboard
    ],
    course_controller: controllers.CourseController = Provide[
        Application.controllers.course_controller
    ],
):
    """Return HTMX partial: full question history for this student."""
    trajectories = dashboard_service._fetch_trajectories(course_id)

    try:
        members = course_controller.get_course_members(course_id)
        info_map = {s.id: {"name": s.name, "email": s.email} for s in members.get("students", [])}
        info = info_map.get(student_id, {})
        student_name = info.get("name", student_id)
        student_email = info.get("email", "")
    except Exception:
        student_name = student_id
        student_email = ""

    conceptual_nodes = dashboard_service._fetch_conceptual_nodes(course_id)

    questions = []
    for e in trajectories:
        if e.user_id != student_id:
            continue
        node = dashboard_service._match_conceptual_node(e.query, conceptual_nodes, e.retrieved_nodes)
        topic = (node.get("label") or node.get("name")) if node else None
        questions.append({
            "query": e.query,
            "interaction_type": e.interaction_type,
            "hint_triggered": e.hint_triggered,
            "hint_text": e.hint_text,
            "retrieved_nodes": [topic] if topic else [],
        })

    return render_template(
        "course/_struggle_detail.html",
        detail_type="student",
        student_id=student_id,
        student_name=student_name,
        student_email=student_email,
        questions=questions,
        rows=[],
        course_id=course_id,
    )
