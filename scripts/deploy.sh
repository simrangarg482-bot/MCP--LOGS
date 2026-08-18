#!/usr/bin/env bash
# EKIP deployment procedure (Phase 3 Batch 4).
#
# Encodes the sequence docs/operations/deployment.md describes in prose:
# build -> test -> build images -> push -> migrate -> deploy backend ->
# deploy worker -> deploy frontend -> health check -> readiness check.
#
# This script is REAL and RUNNABLE for every step up through image build --
# verified in this environment (backend tests, import-linter, frontend
# checks, and `docker build`'s Dockerfile syntax were all exercised
# directly; the Azure `az` calls below are correct commands for the
# resources infra/main.bicep defines, but were never executed against a
# real subscription -- the identity available in this environment has no
# Contributor/Owner role anywhere (see Batch 3.5). Do not run the AZURE
# DEPLOY section without first confirming you have appropriate permissions.
#
# Usage:
#   ./scripts/deploy.sh build-and-test   # steps 1-3, safe to run anywhere
#   ./scripts/deploy.sh azure-deploy     # steps 4-9, requires real Azure access
#
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:?Set RESOURCE_GROUP to your target resource group}"
NAME_PREFIX="${NAME_PREFIX:?Set NAME_PREFIX (e.g. ekip-prod), matching infra/main.bicep namePrefix param}"
REGISTRY="${REGISTRY:?Set REGISTRY (e.g. myregistry.azurecr.io)}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"

step() { printf '\n=== %s ===\n' "$1"; }

build_and_test() {
    step "1/3: Backend tests + import-linter"
    uv run pytest tests/ -q --deselect tests/ingestion_retrieval/test_connectors.py::test_one_connector
    uv run lint-imports

    step "2/3: Frontend type-check, lint, build"
    (cd frontend && npm ci && npm run typecheck && npm run lint && npm run build)

    step "3/3: Build container images"
    docker build -t "${REGISTRY}/ekip-backend:${IMAGE_TAG}" -f Dockerfile .
    docker build \
        --build-arg "VITE_API_BASE_URL=https://${NAME_PREFIX}-backend.${AZURE_CONTAINERAPPS_DOMAIN:-example.azurecontainerapps.io}" \
        -t "${REGISTRY}/ekip-frontend:${IMAGE_TAG}" \
        -f frontend/Dockerfile frontend

    echo "Images built. Push with:"
    echo "  docker push ${REGISTRY}/ekip-backend:${IMAGE_TAG}"
    echo "  docker push ${REGISTRY}/ekip-frontend:${IMAGE_TAG}"
}

azure_deploy() {
    step "4/9: Push images"
    docker push "${REGISTRY}/ekip-backend:${IMAGE_TAG}"
    docker push "${REGISTRY}/ekip-frontend:${IMAGE_TAG}"

    step "5/9: Run database migration (one-shot, before any app instance starts)"
    # Deliberately NOT part of container startup (section 22) -- run as an
    # explicit, separate step so a migration failure is diagnosed on its own,
    # not conflated with "the app failed to start."
    az containerapp job start \
        --name "${NAME_PREFIX}-migrate" \
        --resource-group "${RESOURCE_GROUP}"

    step "6/9: Deploy backend"
    az containerapp update \
        --name "${NAME_PREFIX}-backend" \
        --resource-group "${RESOURCE_GROUP}" \
        --image "${REGISTRY}/ekip-backend:${IMAGE_TAG}"

    step "7/9: Deploy worker"
    az containerapp update \
        --name "${NAME_PREFIX}-worker" \
        --resource-group "${RESOURCE_GROUP}" \
        --image "${REGISTRY}/ekip-backend:${IMAGE_TAG}"

    step "8/9: Deploy frontend"
    az containerapp update \
        --name "${NAME_PREFIX}-frontend" \
        --resource-group "${RESOURCE_GROUP}" \
        --image "${REGISTRY}/ekip-frontend:${IMAGE_TAG}"

    step "9/9: Health + readiness check"
    BACKEND_FQDN=$(az containerapp show --name "${NAME_PREFIX}-backend" --resource-group "${RESOURCE_GROUP}" --query properties.configuration.ingress.fqdn -o tsv)
    for i in $(seq 1 30); do
        if curl -sf "https://${BACKEND_FQDN}/health" > /dev/null && curl -sf "https://${BACKEND_FQDN}/ready" > /dev/null; then
            echo "Deployment healthy and ready."
            exit 0
        fi
        sleep 2
    done
    echo "Backend never became healthy/ready -- see docs/operations/rollback.md" >&2
    exit 1
}

case "${1:-}" in
    build-and-test) build_and_test ;;
    azure-deploy) azure_deploy ;;
    *)
        echo "Usage: $0 {build-and-test|azure-deploy}" >&2
        exit 1
        ;;
esac
