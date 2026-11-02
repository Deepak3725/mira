"""Single-source database schema (SQLite + PostgreSQL).

``statements(driver)`` returns the full ordered DDL for a fresh database at
the current ``SCHEMA_VERSION``. Both drivers describe the same tables;
only the identity column, timestamp defaults, and float type differ.
Tables are ordered so PostgreSQL foreign keys resolve at CREATE time.

SQLite keeps working exactly as before: the generated SQLite strings match
the historical ``init_db`` statements.
"""

from __future__ import annotations

# name -> (columns, constraints); {ID}, {TS}, {REAL} are driver placeholders.
_TABLES: list[tuple[str, list[str], list[str]]] = [
    ("schema_version", [
        "id INTEGER PRIMARY KEY CHECK (id = 1)",
        "version INTEGER NOT NULL",
    ], []),
    ("users", [
        "{ID}",
        "name TEXT NOT NULL",
        "role TEXT NOT NULL",
        "email TEXT NOT NULL UNIQUE",
        "password_hash TEXT",
        "{TS_CREATED}",
    ], []),
    ("twins", [
        "{ID}",
        "user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE",
        "display_name TEXT NOT NULL",
        "persona TEXT NOT NULL",
        "tone TEXT NOT NULL",
        "profile_json TEXT NOT NULL DEFAULT '{}'",
        "authorized_fields_json TEXT NOT NULL DEFAULT '[]'",
        "avatar_seed TEXT NOT NULL",
        "is_public INTEGER NOT NULL DEFAULT 1",
        "conditions_json TEXT NOT NULL DEFAULT '{}'",
        "{TS_CREATED}",
    ], []),
    ("opportunities", [
        "{ID}",
        "owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE",
        "kind TEXT NOT NULL DEFAULT 'job'",
        "title TEXT NOT NULL",
        "description TEXT NOT NULL DEFAULT ''",
        "required_skills_json TEXT NOT NULL DEFAULT '[]'",
        "preferred_experience_years INTEGER",
        "location TEXT NOT NULL DEFAULT ''",
        "remote_ok INTEGER NOT NULL DEFAULT 0",
        "status TEXT NOT NULL DEFAULT 'open'",
        "category TEXT NOT NULL DEFAULT ''",
        "attributes_json TEXT NOT NULL DEFAULT '{}'",
        "{TS_CREATED}",
    ], []),
    ("requests", [
        "{ID}",
        "twin_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE",
        "owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE",
        "category TEXT NOT NULL DEFAULT 'other'",
        "need_text TEXT NOT NULL DEFAULT ''",
        "requirements_json TEXT NOT NULL DEFAULT '{}'",
        "conditions_json TEXT NOT NULL DEFAULT '{}'",
        "status TEXT NOT NULL DEFAULT 'draft'",
        "idempotency_key TEXT NOT NULL DEFAULT ''",
        "expires_at TEXT",
        "quantity_requested INTEGER NOT NULL DEFAULT 1",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], ["UNIQUE (owner_user_id, idempotency_key)"]),
    ("conversations", [
        "{ID}",
        "twin_a_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE",
        "twin_b_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE",
        "opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL",
        "purpose TEXT NOT NULL DEFAULT 'open-chat'",
        "status TEXT NOT NULL DEFAULT 'active'",
        "turn TEXT NOT NULL DEFAULT 'a'",
        "max_turns INTEGER NOT NULL DEFAULT 12",
        "is_demo INTEGER NOT NULL DEFAULT 0",
        "decision_a TEXT",
        "decision_b TEXT",
        "created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "request_id INTEGER REFERENCES requests(id) ON DELETE SET NULL",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], []),
    ("messages", [
        "{ID}",
        "conv_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE",
        "sender TEXT NOT NULL",
        "body TEXT NOT NULL",
        "provider TEXT NOT NULL DEFAULT 'mock'",
        "tokens_in INTEGER NOT NULL DEFAULT 0",
        "tokens_out INTEGER NOT NULL DEFAULT 0",
        "{TS_CREATED}",
    ], []),
    ("audit_events", [
        "{ID}",
        "conv_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE",
        "action TEXT NOT NULL",
        "actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "detail TEXT NOT NULL DEFAULT ''",
        "{TS_CREATED}",
    ], []),
    ("usage_log", [
        "{ID}",
        "provider TEXT NOT NULL",
        "model TEXT NOT NULL",
        "conv_id INTEGER",
        "user_id INTEGER",
        "tokens_in INTEGER NOT NULL DEFAULT 0",
        "tokens_out INTEGER NOT NULL DEFAULT 0",
        "est_cost_usd {REAL} NOT NULL DEFAULT 0",
        "{TS_CREATED}",
        "status TEXT NOT NULL DEFAULT 'ok'",
        "notes TEXT NOT NULL DEFAULT ''",
        "latency_ms INTEGER NOT NULL DEFAULT 0",
        "attempts INTEGER NOT NULL DEFAULT 1",
    ], []),
    ("memories", [
        "{ID}",
        "twin_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE",
        "content TEXT NOT NULL",
        "source TEXT NOT NULL DEFAULT ''",
        "status TEXT NOT NULL DEFAULT 'pending'",
        "category TEXT NOT NULL DEFAULT 'general'",
        "source_time TEXT",
        "{TS_CREATED}",
    ], []),
    ("projects", [
        "{ID}",
        "owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE",
        "title TEXT NOT NULL",
        "goal TEXT NOT NULL DEFAULT ''",
        "spec_json TEXT NOT NULL DEFAULT '{}'",
        "status TEXT NOT NULL DEFAULT 'draft'",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], []),
    ("project_tasks", [
        "{ID}",
        "project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE",
        "kind TEXT NOT NULL DEFAULT 'chat'",
        "title TEXT NOT NULL",
        "instructions TEXT NOT NULL DEFAULT ''",
        "status TEXT NOT NULL DEFAULT 'pending'",
        "deps_json TEXT NOT NULL DEFAULT '[]'",
        "provider TEXT NOT NULL DEFAULT ''",
        "model TEXT NOT NULL DEFAULT ''",
        "result_json TEXT NOT NULL DEFAULT '{}'",
        "attempts INTEGER NOT NULL DEFAULT 0",
        "error TEXT NOT NULL DEFAULT ''",
        "needs_approval INTEGER NOT NULL DEFAULT 0",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], []),
    ("project_files", [
        "{ID}",
        "project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE",
        "path TEXT NOT NULL",
        "version INTEGER NOT NULL DEFAULT 1",
        "content TEXT NOT NULL DEFAULT ''",
        "updated_by TEXT NOT NULL DEFAULT ''",
        "{TS_CREATED}",
    ], ["UNIQUE (project_id, path, version)"]),
    ("project_events", [
        "{ID}",
        "project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE",
        "actor TEXT NOT NULL DEFAULT ''",
        "action TEXT NOT NULL",
        "detail TEXT NOT NULL DEFAULT ''",
        "{TS_CREATED}",
    ], []),
    ("channel_accounts", [
        "{ID}",
        "owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE",
        "channel TEXT NOT NULL",
        "label TEXT NOT NULL DEFAULT ''",
        "credentials_json TEXT NOT NULL DEFAULT '{}'",
        "webhook_secret TEXT NOT NULL DEFAULT ''",
        "status TEXT NOT NULL DEFAULT 'pending'",
        "permissions_json TEXT NOT NULL DEFAULT '{}'",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], []),
    ("channel_contacts", [
        "{ID}",
        "account_id INTEGER NOT NULL REFERENCES channel_accounts(id)"
        " ON DELETE CASCADE",
        "external_id TEXT NOT NULL",
        "display_name TEXT NOT NULL",
        "{TS_CREATED}",
    ], ["UNIQUE (account_id, external_id)"]),
    ("outbox_messages", [
        "{ID}",
        "account_id INTEGER NOT NULL REFERENCES channel_accounts(id)"
        " ON DELETE CASCADE",
        "contact_id INTEGER REFERENCES channel_contacts(id)"
        " ON DELETE SET NULL",
        "body TEXT NOT NULL",
        "status TEXT NOT NULL DEFAULT 'draft'",
        "scheduled_for TEXT",
        "idempotency_key TEXT NOT NULL",
        "attempts INTEGER NOT NULL DEFAULT 0",
        "last_error TEXT NOT NULL DEFAULT ''",
        "provider_msg_id TEXT NOT NULL DEFAULT ''",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], ["UNIQUE (idempotency_key)"]),
    ("channel_events", [
        "{ID}",
        "account_id INTEGER NOT NULL REFERENCES channel_accounts(id)"
        " ON DELETE CASCADE",
        "direction TEXT NOT NULL DEFAULT 'out'",
        "kind TEXT NOT NULL",
        "detail TEXT NOT NULL DEFAULT ''",
        "{TS_CREATED}",
    ], []),
    ("revoked_sessions", [
        "token_hash TEXT PRIMARY KEY",
        "revoked_at TEXT NOT NULL",
    ], []),
    ("matches", [
        "{ID}",
        "search_request_id INTEGER NOT NULL REFERENCES requests(id)"
        " ON DELETE CASCADE",
        "counterpart_type TEXT NOT NULL",
        "counterpart_id INTEGER NOT NULL",
        "status TEXT NOT NULL DEFAULT 'potential'",
        "owner_accepted INTEGER NOT NULL DEFAULT 0",
        "peer_accepted INTEGER NOT NULL DEFAULT 0",
        "score INTEGER NOT NULL DEFAULT 0",
        "reason TEXT NOT NULL DEFAULT ''",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], ["UNIQUE (search_request_id, counterpart_type, counterpart_id)"]),
    ("notifications", [
        "{ID}",
        "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE",
        "kind TEXT NOT NULL",
        "title TEXT NOT NULL",
        "body TEXT NOT NULL DEFAULT ''",
        "link TEXT NOT NULL DEFAULT ''",
        "is_read INTEGER NOT NULL DEFAULT 0",
        "{TS_CREATED}",
    ], []),
    ("match_queue", [
        "{ID}",
        "event_type TEXT NOT NULL",
        "ref_type TEXT NOT NULL",
        "ref_id INTEGER NOT NULL",
        "status TEXT NOT NULL DEFAULT 'pending'",
        "attempts INTEGER NOT NULL DEFAULT 0",
        "next_retry_at TEXT",
        "last_error TEXT NOT NULL DEFAULT ''",
        "{TS_CREATED}",
        "{TS_UPDATED}",
    ], []),
    ("twin_chats", [
        "{ID}",
        "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE",
        "twin_id INTEGER NOT NULL REFERENCES twins(id) ON DELETE CASCADE",
        "role TEXT NOT NULL",
        "body TEXT NOT NULL",
        "kind TEXT NOT NULL DEFAULT 'text'",
        "ref_type TEXT NOT NULL DEFAULT ''",
        "ref_id INTEGER NOT NULL DEFAULT 0",
        "{TS_CREATED}",
    ], []),
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_opp_owner ON opportunities(owner_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_opp_cat_status ON opportunities(category, status)",
    "CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunities(status)",
    "CREATE INDEX IF NOT EXISTS idx_opp_kind ON opportunities(kind)",
    "CREATE INDEX IF NOT EXISTS idx_conv_twins ON conversations(twin_a_id, twin_b_id)",
    "CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_conv ON audit_events(conv_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_conv ON usage_log(conv_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_mem_twin ON memories(twin_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_req_owner ON requests(owner_user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_req_cat ON requests(category, status)",
    "CREATE INDEX IF NOT EXISTS idx_conv_req ON conversations(twin_a_id, twin_b_id, request_id)",
    "CREATE INDEX IF NOT EXISTS idx_proj_owner ON projects(owner_user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_ptask_proj ON project_tasks(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_pfiles_proj ON project_files(project_id, path, version)",
    "CREATE INDEX IF NOT EXISTS idx_pevent_proj ON project_events(project_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_chacc_owner ON channel_accounts(owner_user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_chcon_acc ON channel_contacts(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_acc ON outbox_messages(account_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox_messages(status, scheduled_for)",
    "CREATE INDEX IF NOT EXISTS idx_chevent_acc ON channel_events(account_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_req_expires ON requests(status, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_req_twin ON requests(twin_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_match_search ON matches(search_request_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_match_counter ON matches(counterpart_type, counterpart_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read)",
    "CREATE INDEX IF NOT EXISTS idx_mq_status ON match_queue(status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_twinchat_user ON twin_chats(user_id, id)",
]

_DIALECTS = {
    "sqlite": {
        "{ID}": "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "{TS_CREATED}": "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
        "{TS_UPDATED}": "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
        "{REAL}": "REAL",
    },
    "postgres": {
        "{ID}": "id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
        "{TS_CREATED}": "created_at timestamptz NOT NULL DEFAULT now()",
        "{TS_UPDATED}": "updated_at timestamptz NOT NULL DEFAULT now()",
        "{REAL}": "DOUBLE PRECISION",
    },
}


def _dialect(driver: str) -> dict[str, str]:
    try:
        return _DIALECTS[driver]
    except KeyError:
        raise ValueError(f"Unknown schema driver {driver!r}.") from None


def table_statements(driver: str) -> list[str]:
    """CREATE TABLE statements (idempotent). Driver: sqlite | postgres."""
    dialect = _dialect(driver)
    out = []
    for name, columns, constraints in _TABLES:
        rendered = [c for c in columns]
        for token, replacement in dialect.items():
            rendered = [c.replace(token, replacement) for c in rendered]
        body = ", ".join(rendered + constraints)
        out.append(f"CREATE TABLE IF NOT EXISTS {name} ({body})")
    return out


def index_statements() -> list[str]:
    """CREATE INDEX statements (idempotent, driver-agnostic)."""
    return list(_INDEXES)


def statements(driver: str) -> list[str]:
    """Full ordered DDL for a fresh database. Driver: sqlite | postgres."""
    return table_statements(driver) + index_statements()
