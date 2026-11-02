from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required
from dependency_injector.wiring import inject, Provide
from werkzeug.utils import secure_filename

from app.challenges import UNLOCK_SEQUENCE
from app.containers import Application
from app.services import UserService
from app.surveys import SURVEY_LIST, SURVEYS, submit_survey

app = Blueprint("survey", __name__)


def _challenge_stage_for_survey(survey_id: str) -> int | None:
    """Map a survey id to the single challenge stage its hints should be scoped to."""
    try:
        return UNLOCK_SEQUENCE.index(survey_id)
    except ValueError:
        return None


@app.route("/survey/list")
@login_required
def list_surveys():
    course_id = request.args.get("course_id", "")
    return render_template("survey/list.html", surveys=SURVEY_LIST, course_id=course_id)


@app.route("/survey/<survey_id>")
@login_required
@inject
def show_form(
    survey_id: str,
    user_service: UserService = Provide[Application.services.user],
):
    survey = SURVEYS.get(survey_id)
    if not survey:
        abort(404)

    course_id = request.args.get("course_id", "")
    hints = []
    if course_id:
        try:
            hints = user_service.get_all_course_hints_for_student(
                current_user.id, course_id, challenge_stage=_challenge_stage_for_survey(survey_id)
            )
        except Exception:
            hints = []

    return render_template("survey/form.html", survey=survey, hints=hints, course_id=course_id)


@app.route("/survey/<survey_id>/embed")
@login_required
@inject
def show_form_embed(
    survey_id: str,
    user_service: UserService = Provide[Application.services.user],
):
    survey = SURVEYS.get(survey_id)
    if not survey:
        abort(404)

    course_id = request.args.get("course_id", "")
    hints = []
    if course_id:
        try:
            hints = user_service.get_all_course_hints_for_student(
                current_user.id, course_id, challenge_stage=_challenge_stage_for_survey(survey_id)
            )
        except Exception:
            hints = []

    return render_template("survey/form_embed.html", survey=survey, hints=hints, course_id=course_id)


@app.route("/survey/<survey_id>/submit", methods=["POST"])
@login_required
@inject
def submit(
    survey_id,
    user_service: UserService = Provide[Application.services.user],
):
    survey = SURVEYS.get(survey_id)
    if not survey:
        abort(404)

    evidence_file_bytes = None
    evidence_filename = None
    uploaded = request.files.get("llm_evidence_file")
    if uploaded and uploaded.filename:
        evidence_file_bytes = uploaded.read()
        evidence_filename = secure_filename(uploaded.filename)

    course_id = request.form.get("course_id", "")

    try:
        submit_survey(
            survey=survey,
            form_data=request.form,
            student_name=current_user.name,
            student_email=current_user.email,
            evidence_file=evidence_file_bytes,
            evidence_filename=evidence_filename,
        )
        success = True
        error = None
    except Exception as e:
        success = False
        error = str(e)

    if success and course_id:
        try:
            user_service.record_survey_completion(current_user.id, course_id, survey_id)
        except Exception:
            pass

    return render_template(
        "survey/success.html",
        survey=survey,
        success=success,
        error=error,
        course_id=course_id,
    )
