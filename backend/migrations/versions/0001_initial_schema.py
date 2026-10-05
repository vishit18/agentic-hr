"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-05 15:35:32.582532
"""
from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_type', sa.String(length=20), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('actor_name', sa.String(length=200), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('entity_type', sa.String(length=30), nullable=True),
    sa.Column('entity_id', sa.BigInteger(), nullable=True),
    sa.Column('input_hash', sa.String(length=64), nullable=True),
    sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('prompt_version', sa.String(length=50), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('job_id', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_entity', 'audit_log', ['entity_type', 'entity_id'], unique=False)
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'], unique=False)
    op.create_index(op.f('ix_audit_log_ts'), 'audit_log', ['ts'], unique=False)
    op.create_table('files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('original_name', sa.String(length=500), nullable=False),
    sa.Column('stored_path', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('mime_type', sa.String(length=100), nullable=True),
    sa.Column('origin', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_files_sha256'), 'files', ['sha256'], unique=True)
    op.create_table('jobs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('type', sa.String(length=60), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('dedupe_key', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedupe_key')
    )
    op.create_index('ix_jobs_claim', 'jobs', ['status', 'run_after'], unique=False)
    op.create_index(op.f('ix_jobs_type'), 'jobs', ['type'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_table('candidates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('current_version_id', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=100), nullable=True),
    sa.Column('source_detail', sa.Text(), nullable=True),
    sa.Column('merged_into_id', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['merged_into_id'], ['candidates.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('notes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entity_type', sa.String(length=30), nullable=False),
    sa.Column('entity_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_notes_entity', 'notes', ['entity_type', 'entity_id'], unique=False)
    op.create_table('requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('reference', sa.String(length=30), nullable=False),
    sa.Column('company', sa.String(length=300), nullable=True),
    sa.Column('contact_name', sa.String(length=300), nullable=True),
    sa.Column('contact_email', sa.String(length=320), nullable=True),
    sa.Column('job_title', sa.String(length=300), nullable=True),
    sa.Column('location', sa.String(length=300), nullable=True),
    sa.Column('num_roles', sa.Integer(), server_default='1', nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('raw_text', sa.Text(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reference')
    )
    op.create_index(op.f('ix_requests_company'), 'requests', ['company'], unique=False)
    op.create_index(op.f('ix_requests_status'), 'requests', ['status'], unique=False)
    op.create_table('review_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('entity_type', sa.String(length=30), nullable=True),
    sa.Column('entity_id', sa.BigInteger(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_by', sa.Integer(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution', sa.String(length=50), nullable=True),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_review_items_status'), 'review_items', ['status'], unique=False)
    op.create_index(op.f('ix_review_items_type'), 'review_items', ['type'], unique=False)
    op.create_table('tickets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('reporter_id', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('page_url', sa.Text(), nullable=True),
    sa.Column('record_refs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('logs', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('fix_note', sa.Text(), nullable=True),
    sa.Column('assignee_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['assignee_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['reporter_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('attachments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('request_id', sa.Integer(), nullable=False),
    sa.Column('file_id', sa.Integer(), nullable=True),
    sa.Column('filename', sa.String(length=500), nullable=False),
    sa.Column('uploaded_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], ),
    sa.ForeignKeyConstraint(['request_id'], ['requests.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_attachments_request_id'), 'attachments', ['request_id'], unique=False)
    op.create_table('candidate_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('candidate_id', sa.Integer(), nullable=False),
    sa.Column('file_id', sa.Integer(), nullable=True),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('full_name', sa.String(length=300), nullable=True),
    sa.Column('search_text', sa.Text(), nullable=True),
    sa.Column('search_tsv', postgresql.TSVECTOR(), sa.Computed("to_tsvector('english', coalesce(search_text, ''))", persisted=True), nullable=False),
    sa.Column('auto_linked', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('extraction_status', sa.String(length=30), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ),
    sa.ForeignKeyConstraint(['file_id'], ['files.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_candidate_versions_candidate_id'), 'candidate_versions', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_candidate_versions_full_name'), 'candidate_versions', ['full_name'], unique=False)
    op.create_index('ix_candidate_versions_tsv', 'candidate_versions', ['search_tsv'], unique=False, postgresql_using='gin')
    # candidates <-> candidate_versions point at each other, so this FK is added afterwards
    op.create_foreign_key('fk_candidates_current_version', 'candidates', 'candidate_versions', ['current_version_id'], ['id'])
    op.create_table('request_requirements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('request_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('category', sa.String(length=30), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_snippet', sa.Text(), nullable=True),
    sa.Column('edited_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['edited_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['request_id'], ['requests.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_request_requirements_request_id'), 'request_requirements', ['request_id'], unique=False)
    op.create_table('shortlist_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('request_id', sa.Integer(), nullable=False),
    sa.Column('candidate_id', sa.Integer(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('agent_rank', sa.Integer(), nullable=True),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('score_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('explanation', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['request_id'], ['requests.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_shortlist_items_candidate_id'), 'shortlist_items', ['candidate_id'], unique=False)
    op.create_index(op.f('ix_shortlist_items_request_id'), 'shortlist_items', ['request_id'], unique=False)
    op.create_table('candidate_embeddings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('candidate_version_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('ref', sa.String(length=100), nullable=True),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=768), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_version_id'], ['candidate_versions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_candidate_embeddings_candidate_version_id'), 'candidate_embeddings', ['candidate_version_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_candidate_embeddings_candidate_version_id'), table_name='candidate_embeddings')
    op.drop_table('candidate_embeddings')
    op.drop_index(op.f('ix_shortlist_items_request_id'), table_name='shortlist_items')
    op.drop_index(op.f('ix_shortlist_items_candidate_id'), table_name='shortlist_items')
    op.drop_table('shortlist_items')
    op.drop_index(op.f('ix_request_requirements_request_id'), table_name='request_requirements')
    op.drop_table('request_requirements')
    op.drop_constraint('fk_candidates_current_version', 'candidates', type_='foreignkey')
    op.drop_index('ix_candidate_versions_tsv', table_name='candidate_versions', postgresql_using='gin')
    op.drop_index(op.f('ix_candidate_versions_full_name'), table_name='candidate_versions')
    op.drop_index(op.f('ix_candidate_versions_candidate_id'), table_name='candidate_versions')
    op.drop_table('candidate_versions')
    op.drop_index(op.f('ix_attachments_request_id'), table_name='attachments')
    op.drop_table('attachments')
    op.drop_table('tickets')
    op.drop_index(op.f('ix_review_items_type'), table_name='review_items')
    op.drop_index(op.f('ix_review_items_status'), table_name='review_items')
    op.drop_table('review_items')
    op.drop_index(op.f('ix_requests_status'), table_name='requests')
    op.drop_index(op.f('ix_requests_company'), table_name='requests')
    op.drop_table('requests')
    op.drop_index('ix_notes_entity', table_name='notes')
    op.drop_table('notes')
    op.drop_table('candidates')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_jobs_type'), table_name='jobs')
    op.drop_index('ix_jobs_claim', table_name='jobs')
    op.drop_table('jobs')
    op.drop_index(op.f('ix_files_sha256'), table_name='files')
    op.drop_table('files')
    op.drop_index(op.f('ix_audit_log_ts'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_index('ix_audit_entity', table_name='audit_log')
    op.drop_table('audit_log')
