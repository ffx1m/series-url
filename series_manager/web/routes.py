from flask import Blueprint, render_template, redirect, request, url_for

from series_manager.services.settings_service import load_config, save_config

web_bp = Blueprint("web", __name__)


@web_bp.route("/")
def dashboard_page():
    return render_template("dashboard.html")


@web_bp.route("/video")
def video_page():
    return render_template("video.html")


@web_bp.route("/image")
def image_page():
    return render_template("image.html")


@web_bp.route("/manage")
def manage_page():
    return render_template("manage.html")


@web_bp.route("/queue")
def queue_page():
    return render_template("queue.html")


@web_bp.route("/logs")
def logs_page():
    return redirect(url_for("web.queue_page"))


@web_bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        config = {
            "cloudflare_account_id": request.form.get("cloudflare_account_id", "").strip(),
            "cloudflare_access_key": request.form.get("cloudflare_access_key", "").strip(),
            "cloudflare_secret_key": request.form.get("cloudflare_secret_key", "").strip(),
            "cloudflare_api_token": request.form.get("cloudflare_api_token", "").strip(),
            "r2_bucket_name": request.form.get("r2_bucket_name", "data-series").strip(),
            "worker_domain": request.form.get("worker_domain", "https://series.film01-thirx.workers.dev").strip(),
        }
        save_config(config)
        return redirect(url_for("web.settings_page", success="1"))

    return render_template(
        "settings.html",
        config=load_config(),
        success=request.args.get("success") == "1",
    )
