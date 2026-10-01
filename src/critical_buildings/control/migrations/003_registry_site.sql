-- Control store schema, version 3: each building remembers the site it was last assigned to,
-- so Site IDs stay stable without depending on a previous run's output file.
ALTER TABLE building_registry ADD COLUMN site_id TEXT;
