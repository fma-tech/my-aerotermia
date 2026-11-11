#!/usr/bin/env bash
# Builds dist/my-aerotermia-lambda.zip for AWS Lambda (Python 3.13).
# Works on macOS and Linux: dependencies are downloaded as Linux wheels.
#
#   ./build.sh              -> x86_64 Lambda
#   ARCH=arm64 ./build.sh   -> arm64 Lambda
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
ARCH="${ARCH:-x86_64}"
case "$ARCH" in
  x86_64) PLATFORMS=(--platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64) ;;
  arm64)  PLATFORMS=(--platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64) ;;
  *) echo "ARCH must be x86_64 or arm64" >&2; exit 1 ;;
esac

rm -rf build dist
mkdir -p build/pkg dist

"$PY" -m pip install --quiet --upgrade \
  --target build/pkg "${PLATFORMS[@]}" \
  --implementation cp --python-version 3.13 --only-binary=:all: \
  -r requirements.txt

# boto3 comes with the Lambda runtime; drop caches to keep the zip small
rm -rf build/pkg/boto3 build/pkg/botocore build/pkg/bin
find build/pkg -name "__pycache__" -type d -prune -exec rm -rf {} +

cp src/lambda_function.py build/pkg/
[ -d src/static ] && cp -R src/static build/pkg/
(cd build/pkg && zip -qr9 ../../dist/my-aerotermia-lambda.zip .)
echo "Built dist/my-aerotermia-lambda.zip for $ARCH ($(du -h dist/my-aerotermia-lambda.zip | cut -f1))"
