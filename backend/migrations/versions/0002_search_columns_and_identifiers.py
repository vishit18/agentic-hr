"""search columns and identifiers

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05 15:43:04.570575
"""
from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('candidate_identifiers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('candidate_id', sa.Integer(), nullable=False),
    sa.Column('candidate_version_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('value', sa.String(length=320), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ),
    sa.ForeignKeyConstraint(['candidate_version_id'], ['candidate_versions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_candidate_identifiers_candidate_id'), 'candidate_identifiers', ['candidate_id'], unique=False)
    op.create_index('ix_candidate_identifiers_lookup', 'candidate_identifiers', ['kind', 'value'], unique=False)
    op.add_column('candidate_versions', sa.Column('current_title', sa.String(length=300), nullable=True))
    op.add_column('candidate_versions', sa.Column('years_experience', sa.Float(), nullable=True))
    op.add_column('candidate_versions', sa.Column('location', sa.String(length=300), nullable=True))
    op.add_column('candidate_versions', sa.Column('nationality', sa.String(length=100), nullable=True))
    op.add_column('candidate_versions', sa.Column('skills', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('candidate_versions', sa.Column('certifications', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('candidate_versions', sa.Column('qualifications', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('candidate_versions', sa.Column('industries', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('candidate_versions', sa.Column('titles', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('candidate_versions', sa.Column('education_text', sa.Text(), nullable=True))
    op.create_index('ix_candidate_versions_certs', 'candidate_versions', ['certifications'], unique=False, postgresql_using='gin')
    op.create_index('ix_candidate_versions_industries', 'candidate_versions', ['industries'], unique=False, postgresql_using='gin')
    op.create_index('ix_candidate_versions_skills', 'candidate_versions', ['skills'], unique=False, postgresql_using='gin')
    op.create_index('ix_candidate_embeddings_hnsw', 'candidate_embeddings', ['embedding'], unique=False, postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})


def downgrade() -> None:
    op.drop_index('ix_candidate_embeddings_hnsw', table_name='candidate_embeddings', postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.drop_index('ix_candidate_versions_skills', table_name='candidate_versions', postgresql_using='gin')
    op.drop_index('ix_candidate_versions_industries', table_name='candidate_versions', postgresql_using='gin')
    op.drop_index('ix_candidate_versions_certs', table_name='candidate_versions', postgresql_using='gin')
    op.drop_column('candidate_versions', 'education_text')
    op.drop_column('candidate_versions', 'titles')
    op.drop_column('candidate_versions', 'industries')
    op.drop_column('candidate_versions', 'qualifications')
    op.drop_column('candidate_versions', 'certifications')
    op.drop_column('candidate_versions', 'skills')
    op.drop_column('candidate_versions', 'nationality')
    op.drop_column('candidate_versions', 'location')
    op.drop_column('candidate_versions', 'years_experience')
    op.drop_column('candidate_versions', 'current_title')
    op.drop_index('ix_candidate_identifiers_lookup', table_name='candidate_identifiers')
    op.drop_index(op.f('ix_candidate_identifiers_candidate_id'), table_name='candidate_identifiers')
    op.drop_table('candidate_identifiers')
