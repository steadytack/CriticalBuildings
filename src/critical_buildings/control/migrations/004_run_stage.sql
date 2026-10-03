-- Control store schema, version 4: what each stage of a run did (status, timing, counts), for the Pipeline and Runs views.
CREATE TABLE run_stage (
    run_id        TEXT NOT NULL REFERENCES run(run_id),
    stage         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at    TEXT,
    finished_at   TEXT,
    detail_json   TEXT NOT NULL DEFAULT '{}',       -- counts and notes the stage recorded, plus seconds
    PRIMARY KEY (run_id, stage)
);
