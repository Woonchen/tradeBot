"""position exit reason

部位平倉原因（停損、停利、訊號、手動、熔斷、強平、交易所外部平倉）。既有的已平倉部位留空。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('exit_reason', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_column('exit_reason')
