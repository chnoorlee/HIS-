"""Initial hospital voice documentation schema."""
from alembic import op
import sqlalchemy as sa

from app.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    # Early synthetic development databases predate capture clock metadata.
    columns = {c["name"] for c in sa.inspect(bind).get_columns("audio_chunks")}
    if "clock_metadata" not in columns:
        op.add_column("audio_chunks", sa.Column("clock_metadata", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    raise RuntimeError("Clinical schema downgrade requires a reviewed retention and recovery plan")
