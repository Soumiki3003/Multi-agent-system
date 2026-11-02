from pathlib import Path

from dotenv import load_dotenv
from flask import current_app, Flask
from flask_login import LoginManager

from app.containers import Application
from app.views import auth, course, knowledge, survey


login_manager = LoginManager()
login_manager.login_view = "auth.login_page"


@login_manager.user_loader
def load_user(user_id: str):
    if not user_id:
        return None
    try:
        container = current_app.container
        user_service = container.services.user()
        user = user_service.get_user(user_id)
        return user
    except Exception as e:
        # Log the error but don't crash the app
        import logging

        logging.getLogger(__name__).error(f"Error loading user {user_id}: {e}")
        return None


def create_app():
    container = Application()
    container.init_resources()
    container.wire(
        modules=[
            "app.views.knowledge",
            "app.views.auth",
            "app.views.course",
            "app.views.survey",
        ]
    )

    app = Flask(__name__)
    app.template_folder = Path(__file__).parent / "app" / "templates" / "web"
    app.secret_key = container.config.flask.secret_key()
    app.container = container

    login_manager.init_app(app)

    app.register_blueprint(knowledge.app, url_prefix="/knowledge")
    app.register_blueprint(auth.app, url_prefix="/auth")
    app.register_blueprint(course.app, url_prefix="/")
    app.register_blueprint(survey.app, url_prefix="/")

    return app


def main():
    load_dotenv()
    create_app().run(host="0.0.0.0", debug=True, threaded=True)


# === Run Server ===
if __name__ == "__main__":
    main()
