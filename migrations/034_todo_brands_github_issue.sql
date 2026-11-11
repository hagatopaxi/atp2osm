-- The GitHub issue or pull request tracking a missing brand, and its state as
-- last read from GitHub (NULL until GitHub has answered once).
ALTER TABLE todo_brands ADD COLUMN IF NOT EXISTS github_issue TEXT;
ALTER TABLE todo_brands ADD COLUMN IF NOT EXISTS issue_state TEXT
    CHECK (issue_state IN ('open', 'closed', 'merged'));
ALTER TABLE todo_brands ADD COLUMN IF NOT EXISTS issue_checked_at TIMESTAMPTZ;
