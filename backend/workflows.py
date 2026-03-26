"""
WorkflowExecutor — runs sequential tool chains independently of the chat loop.
Calls exec_tool() from tools.py without modifying the chat agent.
"""
import json
import re
import time
import uuid

import database as db
from tools import exec_tool


class WorkflowExecutor:
    def __init__(self, http, events):
        self.http = http
        self.events = events

    def _substitute(self, value, input_text: str, step_results: list):
        """Replace {{input}} and {{steps.N.result}} in strings or nested dicts."""
        if isinstance(value, dict):
            return {k: self._substitute(v, input_text, step_results) for k, v in value.items()}
        if isinstance(value, list):
            return [self._substitute(v, input_text, step_results) for v in value]
        if isinstance(value, str):
            result = value.replace("{{input}}", input_text)
            for match in re.finditer(r"\{\{steps\.(\d+)\.result\}\}", result):
                idx = int(match.group(1))
                replacement = step_results[idx] if idx < len(step_results) else ""
                result = result.replace(match.group(0), replacement)
            return result
        return value

    async def run(self, run_id: str, workflow: dict, input_text: str, conv_id: str = None):
        pseudo_conv = conv_id or f"wf-run-{run_id}"
        steps = workflow.get("steps", [])
        if isinstance(steps, str):
            steps = json.loads(steps)
        step_results = []
        all_step_data = []

        await db.update_workflow_run(run_id, status="running", started_at=time.time())
        await self.events.emit(pseudo_conv, "workflow_start", {
            "workflow": workflow["name"], "total_steps": len(steps)
        })

        for i, step in enumerate(steps):
            step_name = step.get("name", f"Step {i}")
            await self.events.emit(pseudo_conv, "workflow_step", {
                "step": i, "name": step_name,
                "status": "running", "total": len(steps)
            })

            tool_name = step["tool"]
            raw_args = step.get("args", {})
            resolved_args = self._substitute(raw_args, input_text, step_results)

            step_record = {
                "step_index": i, "name": step_name,
                "tool": tool_name, "started_at": time.time(), "status": "running"
            }

            try:
                result = await exec_tool(
                    self.http, self.events, tool_name, resolved_args,
                    pseudo_conv
                )
                step_record["result"] = result
                step_record["status"] = "completed"
                step_record["completed_at"] = time.time()
                step_results.append(result)

                await self.events.emit(pseudo_conv, "workflow_step", {
                    "step": i, "name": step_name,
                    "status": "completed", "total": len(steps),
                    "preview": result[:200] if result else ""
                })
            except Exception as e:
                step_record["status"] = "failed"
                step_record["error"] = str(e)
                step_record["completed_at"] = time.time()
                step_results.append(f"ERROR: {e}")
                all_step_data.append(step_record)

                await db.update_workflow_run(
                    run_id, status="failed", error=str(e),
                    step_results=json.dumps(all_step_data),
                    completed_at=time.time()
                )
                await self.events.emit(pseudo_conv, "workflow_error", {
                    "step": i, "error": str(e)
                })
                return all_step_data

            all_step_data.append(step_record)
            await db.update_workflow_run(run_id, step_results=json.dumps(all_step_data))

        await db.update_workflow_run(
            run_id, status="completed",
            step_results=json.dumps(all_step_data),
            completed_at=time.time()
        )
        await self.events.emit(pseudo_conv, "workflow_complete", {
            "workflow": workflow["name"], "steps_completed": len(steps)
        })
        return all_step_data
