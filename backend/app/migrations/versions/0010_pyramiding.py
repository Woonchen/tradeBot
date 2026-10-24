"""pyramiding

策略的加碼設定（全部留空 = 不加碼，既有策略行為不變），部位與影子交易記錄加碼次數、最後成交價。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-25 01:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('strategy_configs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pyramid_atr', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('pyramid_max', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('pyramid_frac', sa.Float(), nullable=True))
    for table in ('positions', 'shadow_trades'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('adds', sa.Integer(), server_default='0', nullable=False))
            batch_op.add_column(sa.Column('last_fill_price', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for table in ('shadow_trades', 'positions'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column('last_fill_price')
            batch_op.drop_column('adds')
    with op.batch_alter_table('strategy_configs', schema=None) as batch_op:
        batch_op.drop_column('pyramid_frac')
        batch_op.drop_column('pyramid_max')
        batch_op.drop_column('pyramid_atr')
