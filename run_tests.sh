#!/bin/bash
# Quick test runner for local development
set -e

echo "Running Xomtracks Lambda + Extractor Tests"
echo "==========================================="

if ! command -v pytest &> /dev/null; then
    echo "pytest not found. Installing..."
    pip install pytest pytest-cov pytest-asyncio moto boto3
fi

if [ -z "$1" ]; then
    echo "Running all tests..."
    pytest tests/ -v --tb=short
else
    echo "Running tests for: $1"
    pytest tests/test_$1.py -v --tb=short
fi

echo ""
echo "Tests complete."
