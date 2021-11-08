#!/bin/bash
set -e

echo "running flake8"
flake8 sciencebeam_pipelines tests setup.py

echo "running pylint"
pylint sciencebeam_pipelines tests setup.py

echo "running pytest"
pytest -p no:cacheprovider

echo "done"
