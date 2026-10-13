"""Persist default duplicate choice and index paged review queries."""

import sqlalchemy as sa
from alembic import op

revision = "20261013_review_integrity"
down_revision = "19382d62dd81"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("duplicate_members")}
    if "is_default_keep" not in columns:
        op.add_column(
            "duplicate_members",
            sa.Column("is_default_keep", sa.Boolean(), nullable=False, server_default="0"),
        )
    # The revision is not stamped until complete. This also recovers a crash just after ADD COLUMN.
    op.execute("UPDATE duplicate_members SET is_default_keep = is_keep")
    indices = {index["name"] for index in inspector.get_indexes("media_files")}
    if "ix_media_status_mtime" not in indices:
        op.create_index("ix_media_status_mtime", "media_files", ["status", "mtime_ns", "id"])
    if "ix_media_library_status_mtime" not in indices:
        op.create_index(
            "ix_media_library_status_mtime",
            "media_files",
            ["library_id", "status", "mtime_ns", "id"],
        )


def downgrade():
    op.drop_index("ix_media_library_status_mtime", table_name="media_files")
    op.drop_index("ix_media_status_mtime", table_name="media_files")
    op.drop_column("duplicate_members", "is_default_keep")
