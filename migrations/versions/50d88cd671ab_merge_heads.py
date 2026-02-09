"""merge heads

Revision ID: 50d88cd671ab
Revises: 4783f54c39ca, fe5db8079aad
Create Date: 2026-02-09 17:54:02.242729
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = '50d88cd671ab'
down_revision = ('4783f54c39ca', 'fe5db8079aad')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Downgrade not supported")
