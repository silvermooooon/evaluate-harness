# Local TheAgentCompany ownCloud environment

This subset uses the original `servers-owncloud:1.0.0` image and its seeded data.
The project and container are named `tac-eval` and `tac-eval-owncloud`. The
existing PostgreSQL, Redis, MySQL and Duet services are outside this project.

Start the service with `docker compose -f infra/compose.yml -p tac-eval up -d`.
Run `python3 infra/controller.py` for the control endpoints on port 2999.
The task container must be able to reach the host through
`SERVER_HOSTNAME=host.docker.internal`; the original `/utils/init.sh` maps that
address to `the-agent-company.com` and calls the original `/utils/reset.sh`.

The local controller implements the upstream `POST /api/reset-owncloud` and
`HEAD`/`GET /api/healthcheck/owncloud` endpoints. Reset stops and removes only the
evaluation ownCloud container, removes its anonymous volume, and starts a fresh
container from the original image. It returns success only after Docker succeeds;
the unchanged task reset script performs the readiness polling.

Deployment differences from upstream:

- Docker Desktop host networking is disabled. A host controller replaces the
  official multi-service controller container; the task init/reset scripts and
  scoring code are unchanged.
- Only ownCloud is deployed. The selected tasks need file access and file updates,
  which use ownCloud's existing WebDAV/API. Collabora's in-browser office editor,
  GitLab, Plane, RocketChat and NPCs are not deployed or represented by mocks.
- The original ownCloud service configuration is retained, with a separate
  container name. `linux/amd64` explicitly selects the upstream image architecture
  on this arm64 host; both agents use the same environment.
- Removing the anonymous volume during reset cleans up discarded evaluation data;
  this does not touch named volumes or any unrelated service.

This deployment is a local file-workflow subset, not the full 175-task benchmark.
The selected tasks use deterministic graders, so no environment-model API calls
are needed. The model performing a task must not read `/utils`, the upstream
task-source checkout, or evaluation reports.
