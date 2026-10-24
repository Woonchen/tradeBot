"""drawdown breaker

設定頁的「回撤熔斷」：權益從最高點回撤超過上限就全部平倉並停用 Bot。預設 0 = 不啟用，既有行為不變。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-24 17:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('bot_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_drawdown_pct', sa.Float(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('equity_peaks', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('bot_settings', schema=None) as batch_op:
        batch_op.drop_column('equity_peaks')
        batch_op.drop_column('max_drawdown_pct')
