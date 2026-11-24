.PHONY: install lint typecheck test eval eval-live web run server tf-init tf-validate tf-plan tf-apply deploy-agent-engine deploy-cloud-run

PYTHON ?= python3
TF_DIR := deployment/terraform

install:
	uv sync --extra dev || pip install -e ".[dev]"

lint:
	ruff check app tests deployment

typecheck:
	mypy app

test:
	pytest tests/unit tests/integration tests/eval/test_golden_eval.py

eval:
	$(PYTHON) -m tests.eval.run_eval --mode offline

eval-live:
	adk eval app tests/eval/evalsets/sre_incident_golden.evalset.json --config_file_path tests/eval/eval_config.json

web:
	adk web .

run:
	adk run app

server:
	uvicorn app.server:app --host 0.0.0.0 --port 8080 --reload

tf-init:
	terraform -chdir=$(TF_DIR) init

tf-validate:
	terraform -chdir=$(TF_DIR) fmt -check -recursive
	terraform -chdir=$(TF_DIR) validate

tf-plan:
	terraform -chdir=$(TF_DIR) plan -var-file=terraform.tfvars

tf-apply:
	terraform -chdir=$(TF_DIR) apply -var-file=terraform.tfvars

deploy-agent-engine:
	$(PYTHON) -m deployment.deploy_agent_engine \
		--project "$(GOOGLE_CLOUD_PROJECT)" \
		--location "$(or $(GOOGLE_CLOUD_LOCATION),us-central1)" \
		--staging-bucket "gs://$(GOOGLE_CLOUD_PROJECT)-sentinel-sre-agent-artifacts"

deploy-cloud-run:
	adk deploy cloud_run \
		--project "$(GOOGLE_CLOUD_PROJECT)" \
		--region "$(or $(GOOGLE_CLOUD_LOCATION),us-central1)" \
		--service_name sentinel-sre-agent \
		--app_name app \
		--trace_to_cloud \
		app
