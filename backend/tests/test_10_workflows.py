"""
Tests for Workflow CRUD, execution, schedules, and webhooks.
"""
import pytest
import time


class TestWorkflowCRUD:
    def test_create_workflow(self, client):
        r = client.post("/api/workflows", json={
            "name": "Temp Workflow",
            "description": "temporary",
            "steps": [
                {"name": "Step 1", "type": "tool", "tool": "execute_code",
                 "args": {"code": "print('temp')", "language": "python"}}
            ]
        })
        assert r.status_code == 200
        wf = r.json()
        assert "id" in wf
        assert wf["name"] == "Temp Workflow"
        client.delete(f"/api/workflows/{wf['id']}")

    def test_list_workflows(self, client, created_workflow):
        r = client.get("/api/workflows")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        ids = [w["id"] for w in data]
        assert created_workflow["id"] in ids

    def test_get_workflow(self, client, created_workflow):
        wf_id = created_workflow["id"]
        r = client.get(f"/api/workflows/{wf_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == wf_id
        assert "steps" in data

    def test_update_workflow(self, client, created_workflow):
        wf_id = created_workflow["id"]
        r = client.put(f"/api/workflows/{wf_id}", json={
            "description": "Updated description"
        })
        assert r.status_code == 200

    def test_delete_nonexistent_workflow(self, client):
        r = client.delete("/api/workflows/wf-nonexistent999")
        assert r.status_code in (200, 404)


class TestWorkflowExecution:
    def test_run_workflow(self, long_client, created_workflow):
        wf_id = created_workflow["id"]
        r = long_client.post(f"/api/workflows/{wf_id}/run", json={
            "input": "test input"
        })
        assert r.status_code == 200
        data = r.json()
        assert "run_id" in data
        run_id = data["run_id"]

        # Poll for completion (max 60s)
        for _ in range(12):
            time.sleep(5)
            run = long_client.get(f"/api/workflow-runs/{run_id}").json()
            if run["status"] in ("completed", "failed"):
                break

        assert run["status"] == "completed", f"Workflow run failed: {run.get('error', '')}"
        assert len(run.get("step_results", [])) > 0

    def test_list_workflow_runs(self, client, created_workflow):
        wf_id = created_workflow["id"]
        r = client.get(f"/api/workflows/{wf_id}/runs")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_get_nonexistent_run(self, client):
        r = client.get("/api/workflow-runs/wfr-nonexistent999")
        assert r.status_code in (200, 404)


class TestWorkflowWebhook:
    def test_trigger_webhook(self, long_client, created_workflow):
        wf = long_client.get(f"/api/workflows/{created_workflow['id']}").json()
        webhook_id = wf.get("webhook_id")
        if not webhook_id:
            pytest.skip("Workflow has no webhook_id")

        r = long_client.post(f"/api/webhooks/workflow/{webhook_id}", json={
            "data": "webhook test"
        })
        assert r.status_code == 200
        data = r.json()
        assert "run_id" in data

    def test_trigger_nonexistent_webhook(self, client):
        r = client.post("/api/webhooks/workflow/nonexistent-hook-id", json={})
        assert r.status_code == 404


class TestWorkflowSchedules:
    def test_create_schedule(self, client, created_workflow):
        wf_id = created_workflow["id"]
        r = client.post("/api/workflow-schedules", json={
            "workflow_id": wf_id,
            "cron_expr": "0 */6 * * *",
            "input_template": "scheduled run"
        })
        assert r.status_code == 200
        sched = r.json()
        assert "id" in sched
        # Cleanup
        client.delete(f"/api/workflow-schedules/{sched['id']}")

    def test_list_schedules(self, client):
        r = client.get("/api/workflow-schedules")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    def test_list_schedules_filtered(self, client, created_workflow):
        wf_id = created_workflow["id"]
        r = client.get("/api/workflow-schedules", params={"workflow_id": wf_id})
        assert r.status_code == 200

    def test_update_schedule(self, client, created_workflow):
        wf_id = created_workflow["id"]
        # Create one to update
        sched = client.post("/api/workflow-schedules", json={
            "workflow_id": wf_id,
            "cron_expr": "30 2 * * *",
            "input_template": "original"
        }).json()
        sid = sched["id"]

        r = client.put(f"/api/workflow-schedules/{sid}", json={
            "cron_expr": "0 3 * * *",
            "enabled": False
        })
        assert r.status_code == 200
        # Cleanup
        client.delete(f"/api/workflow-schedules/{sid}")

    def test_delete_nonexistent_schedule(self, client):
        r = client.delete("/api/workflow-schedules/ws-nonexistent999")
        assert r.status_code in (200, 404)


class TestSeedWorkflows:
    def test_seed_workflows(self, client):
        r = client.post("/api/seed/workflows")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, (list, dict))
