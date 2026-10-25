"""breaker fallback leverage

回撤熔斷後自動降槓桿、繼續運作。預設 0 = 不使用（熔斷就停用 Bot，跟原本一樣）。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-25 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('bot_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('breaker_fallback_leverage', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('bot_settings', schema=None) as batch_op:
        batch_op.drop_column('breaker_fallback_leverage')
