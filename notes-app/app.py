"""
Student Notes & Study Asset Manager - Flask backend.

Run locally:   python app.py   then open http://localhost:5000
Run on EC2:    gunicorn -w 2 -b 127.0.0.1:8000 app:app   (behind nginx)
"""
import logging
import uuid
from datetime import datetime, timezone

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

# ---------- logging (stdout + optional file for the CloudWatch agent) ----------
handlers = [logging.StreamHandler()]
if app.config["LOG_FILE"]:
    handlers.append(logging.FileHandler(app.config["LOG_FILE"]))
logging.basicConfig(level=logging.INFO, handlers=handlers,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notes-app")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


def now():
    return datetime.now(timezone.utc)


# ------------------------------- models ---------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    notes = db.relationship("Note", backref="owner", cascade="all, delete-orphan")


class Note(db.Model):
    __tablename__ = "notes"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(100), default="")
    content = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now)
    attachments = db.relationship("Attachment", backref="note", cascade="all, delete-orphan")


class Attachment(db.Model):
    __tablename__ = "attachments"
    id = db.Column(db.Integer, primary_key=True)
    note_id = db.Column(db.Integer, db.ForeignKey("notes.id"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    s3_key = db.Column(db.String(512), nullable=False)   # the file lives in S3, not in the DB
    size_bytes = db.Column(db.BigInteger, default=0)
    uploaded_at = db.Column(db.DateTime(timezone=True), default=now)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ------------------------------- S3 helpers -----------------------------------
_s3 = None


def s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config as BotoConfig
        _s3 = boto3.client("s3", region_name=app.config["AWS_REGION"],
                           config=BotoConfig(signature_version="s3v4"))
    return _s3


def uploads_enabled():
    return bool(app.config["S3_BUCKET"])


def own_note_or_404(note_id):
    note = db.session.get(Note, note_id)
    if not note or note.user_id != current_user.id:
        abort(404)
    return note


# ------------------------------- auth routes ----------------------------------
@app.route("/")
def index():
    return redirect(url_for("notes" if current_user.is_authenticated else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or len(password) < 6:
            flash("Fill all fields. Password must be at least 6 characters.", "error")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered.", "error")
        else:
            user = User(name=name, email=email, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            log.info("New user registered: %s", email)
            login_user(user)
            return redirect(url_for("notes"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, request.form.get("password", "")):
            login_user(user)
            return redirect(url_for("notes"))
        log.warning("Failed login for %s", email)
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ------------------------------- notes routes ---------------------------------
@app.route("/notes")
@login_required
def notes():
    q = request.args.get("q", "").strip()
    subject = request.args.get("subject", "").strip()
    query = Note.query.filter_by(user_id=current_user.id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Note.title.ilike(like), Note.content.ilike(like)))
    if subject:
        query = query.filter_by(subject=subject)
    items = query.order_by(Note.updated_at.desc()).all()
    subjects = sorted({s for (s,) in db.session.query(Note.subject)
                       .filter(Note.user_id == current_user.id).distinct() if s})
    return render_template("notes.html", notes=items, q=q, subject=subject, subjects=subjects)


@app.route("/notes/new", methods=["GET", "POST"])
@login_required
def new_note():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Title is required.", "error")
            return render_template("note_form.html", note=None)
        note = Note(user_id=current_user.id, title=title,
                    subject=request.form.get("subject", "").strip(),
                    content=request.form.get("content", ""))
        db.session.add(note)
        db.session.commit()
        return redirect(url_for("view_note", note_id=note.id))
    return render_template("note_form.html", note=None)


@app.route("/notes/<int:note_id>")
@login_required
def view_note(note_id):
    note = own_note_or_404(note_id)
    return render_template("note_view.html", note=note, uploads_enabled=uploads_enabled(),
                           max_mb=app.config["MAX_UPLOAD_MB"])


@app.route("/notes/<int:note_id>/edit", methods=["GET", "POST"])
@login_required
def edit_note(note_id):
    note = own_note_or_404(note_id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Title is required.", "error")
        else:
            note.title = title
            note.subject = request.form.get("subject", "").strip()
            note.content = request.form.get("content", "")
            db.session.commit()
            return redirect(url_for("view_note", note_id=note.id))
    return render_template("note_form.html", note=note)


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id):
    note = own_note_or_404(note_id)
    if uploads_enabled():
        for a in note.attachments:
            s3().delete_object(Bucket=app.config["S3_BUCKET"], Key=a.s3_key)
    db.session.delete(note)
    db.session.commit()
    flash("Note deleted.", "ok")
    return redirect(url_for("notes"))


# ---------------------------- attachment routes -------------------------------
# Flow: browser asks for a presigned POST -> uploads the file straight to S3
#       -> tells the app "done" so it saves the metadata row in RDS.
@app.route("/notes/<int:note_id>/upload-url", methods=["POST"])
@login_required
def upload_url(note_id):
    note = own_note_or_404(note_id)
    if not uploads_enabled():
        return jsonify(error="Uploads are not configured yet (no S3 bucket)."), 400
    filename = secure_filename((request.json or {}).get("filename", "")) or "file"
    key = f"users/{current_user.id}/notes/{note.id}/{uuid.uuid4().hex}-{filename}"
    max_bytes = app.config["MAX_UPLOAD_MB"] * 1024 * 1024
    post = s3().generate_presigned_post(
        Bucket=app.config["S3_BUCKET"], Key=key,
        Conditions=[["content-length-range", 1, max_bytes]], ExpiresIn=300)
    return jsonify(url=post["url"], fields=post["fields"], key=key, filename=filename)


@app.route("/notes/<int:note_id>/attachments", methods=["POST"])
@login_required
def save_attachment(note_id):
    note = own_note_or_404(note_id)
    data = request.json or {}
    key = data.get("key", "")
    if not key.startswith(f"users/{current_user.id}/notes/{note.id}/"):
        abort(400)
    head = s3().head_object(Bucket=app.config["S3_BUCKET"], Key=key)  # make sure it really uploaded
    att = Attachment(note_id=note.id, filename=data.get("filename", "file"),
                     s3_key=key, size_bytes=head["ContentLength"])
    db.session.add(att)
    db.session.commit()
    log.info("Attachment uploaded: note=%s key=%s", note.id, key)
    return jsonify(ok=True)


def own_attachment_or_404(att_id):
    att = db.session.get(Attachment, att_id)
    if not att or att.note.user_id != current_user.id:
        abort(404)
    return att


@app.route("/attachments/<int:att_id>/download")
@login_required
def download_attachment(att_id):
    att = own_attachment_or_404(att_id)
    url = s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": app.config["S3_BUCKET"], "Key": att.s3_key,
                "ResponseContentDisposition": f'attachment; filename="{att.filename}"'},
        ExpiresIn=300)
    return redirect(url)


@app.route("/attachments/<int:att_id>/delete", methods=["POST"])
@login_required
def delete_attachment(att_id):
    att = own_attachment_or_404(att_id)
    note_id = att.note_id
    s3().delete_object(Bucket=app.config["S3_BUCKET"], Key=att.s3_key)
    db.session.delete(att)
    db.session.commit()
    return redirect(url_for("view_note", note_id=note_id))


# ------------------------------- health check ---------------------------------
@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify(status="ok"), 200
    except Exception as e:  # noqa: BLE001
        log.error("Health check failed: %s", e)
        return jsonify(status="db_error"), 500


@app.template_filter("size")
def human_size(n):
    n = n or 0
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
