#!/usr/bin/env bash
# Render build step. Exit on any failure so a broken migration fails the deploy
# rather than shipping a half-updated app.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

python manage.py collectstatic --no-input
python manage.py migrate
