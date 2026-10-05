-- Control store schema, version 5: run history and the identity registries now live in the operational state database
-- (data/state.db). ControlStore copies any rows across before running this, then these tables are dropped.
DROP TABLE run_stage;
DROP TABLE run_change;
DROP TABLE release;
DROP TABLE run;
DROP TABLE building_registry;
DROP TABLE site_registry;
