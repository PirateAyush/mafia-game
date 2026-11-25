#!/bin/sh
# Runs on the server, after the code there has been updated to the latest master.
# Started by the deploy job in .github/workflows/ci.yml, over SSH.
set -eu

docker compose up --build --detach --remove-orphans
docker image prune --force  # delete old images so the disk doesn't slowly fill up
docker compose ps
