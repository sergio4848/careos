-- Creates the isolated database used by `docker compose run --rm api pytest`.
-- Runs only on first initialisation of the postgres volume.
CREATE DATABASE careos_test;
