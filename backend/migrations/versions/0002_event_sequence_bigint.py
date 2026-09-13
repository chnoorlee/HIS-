"""Store microsecond event cursors without overflowing PostgreSQL integers."""

from alembic import op
import sqlalchemy as sa


revision = "0002_event_sequence_bigint"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns("outbox_events")
    }
    if isinstance(columns["sequence"]["type"], sa.BigInteger):
        return
    with op.batch_alter_table("outbox_events") as batch:
        batch.alter_column(
            "sequence",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )


def downgrade() -> None:
    raise RuntimeError("Event cursors cannot be safely narrowed to 32-bit integers")
