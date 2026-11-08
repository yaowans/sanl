CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL DEFAULT 'unknown',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fetched_at INTEGER,
    last_status INTEGER DEFAULT 0,
    node_count INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscribe_url TEXT NOT NULL,
    source_id INTEGER,
    node_name TEXT,
    node_type TEXT NOT NULL,
    node_data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    country TEXT,
    provider TEXT,
    latency INTEGER,
    download_speed INTEGER,
    score REAL DEFAULT 0.0,
    last_checked_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_nodes_subscribe ON nodes(subscribe_url);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_country ON nodes(country);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);

CREATE TABLE IF NOT EXISTS check_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    error_message TEXT,
    started_at INTEGER,
    finished_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

-- ===== Phase 2: Token & User 系统 =====

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    token TEXT NOT NULL UNIQUE,
    name TEXT DEFAULT 'default',
    permissions TEXT DEFAULT 'read',
    is_active INTEGER NOT NULL DEFAULT 1,
    expired_at INTEGER,
    last_used_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tokens_token ON tokens(token);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- ===== 账号密码登录会话 =====

CREATE TABLE IF NOT EXISTS user_sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_at);

-- 登录失败计数（按 IP+用户名，防撞库）
CREATE TABLE IF NOT EXISTS login_failures (
    key TEXT PRIMARY KEY,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_fail_at INTEGER NOT NULL
);

-- ===== GFW 封锁检测结果 =====

CREATE TABLE IF NOT EXISTS gfw_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server TEXT NOT NULL,
    port INTEGER NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,      -- 1=被墙/国内不可达
    checkable INTEGER NOT NULL DEFAULT 1,    -- 0=检测通道不支持该目标（结果不可信）
    source TEXT,                             -- 检测通道标识（ping0/iptools/...）
    detail TEXT,                             -- 原始返回摘要
    latency INTEGER,
    checked_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_gfw_server_port ON gfw_checks(server, port);
CREATE INDEX IF NOT EXISTS idx_gfw_checked ON gfw_checks(checked_at);

-- 节点级 GFW 结论缓存（避免每次测速都重复探测）
CREATE TABLE IF NOT EXISTS node_gfw_status (
    node_id INTEGER PRIMARY KEY,
    server TEXT NOT NULL,
    port INTEGER NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,
    checkable INTEGER NOT NULL DEFAULT 1,
    source TEXT,
    checked_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_node_gfw_server_port ON node_gfw_status(server, port);