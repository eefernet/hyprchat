"""
WorkflowExecutor — automation engine for deterministic tool chains.
Supports: sequential, parallel, loop, AI completion, sub-workflow composition,
conditionals, retry/error handling, and named variables.
"""
import asyncio
import json
import re
import time

import config
import database as db
from tools import exec_tool


# ---------------------------------------------------------------------------
# Minimal cron parser (no external deps)
# Supports: *, */N, N, N,M,O and ranges N-M for each of 5 fields
# Fields: minute hour day_of_month month day_of_week
# ---------------------------------------------------------------------------
def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of valid integer values."""
    values = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            values.update(range(min_val, max_val + 1, step))
        elif "-" in part:
            lo, hi = part.split("-", 1)
            values.update(range(int(lo), int(hi) + 1))
        else:
            values.add(int(part))
    return values


def next_cron_time(cron_expr: str, after: float = None) -> float:
    """Return the next Unix timestamp matching cron_expr after the given time."""
    from datetime import datetime, timedelta
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression (need 5 fields): {cron_expr}")

    minutes = _parse_cron_field(parts[0], 0, 59)
    hours   = _parse_cron_field(parts[1], 0, 23)
    days    = _parse_cron_field(parts[2], 1, 31)
    months  = _parse_cron_field(parts[3], 1, 12)
    cron_dows = _parse_cron_field(parts[4], 0, 6)  # cron: 0=Sun,1=Mon..6=Sat

    # Convert cron DOW to Python weekday (0=Mon..6=Sun)
    # cron 0(Sun)->py 6, cron 1(Mon)->py 0, cron 2(Tue)->py 1, etc.
    py_dows = {(d - 1) % 7 for d in cron_dows}

    dt = datetime.utcfromtimestamp(after or time.time()) + timedelta(minutes=1)
    dt = dt.replace(second=0, microsecond=0)

    # Search up to 366 days ahead
    for _ in range(366 * 24 * 60):
        if (dt.month in months and dt.day in days and
                dt.weekday() in py_dows and
                dt.hour in hours and dt.minute in minutes):
            return dt.timestamp()
        dt += timedelta(minutes=1)
    # Fallback: 1 hour from now
    return time.time() + 3600
# Cron uses 0=Sunday..6=Saturday. We handle this in the loop above.


class WorkflowExecutor:
    def __init__(self, http, events):
        self.http = http
        self.events = events

    # ------------------------------------------------------------------
    # Variable substitution
    # ------------------------------------------------------------------
    def _substitute(self, value, ctx: dict):
        """Replace template variables in strings or nested structures.

        Supported patterns:
            {{input}}           — workflow input text
            {{steps.N.result}}  — result from step N (0-indexed)
            {{vars.name}}       — named variable
            {{loop.item}}       — current loop item
            {{loop.index}}      — current loop index
            {{webhook.field}}   — webhook payload field
        """
        if isinstance(value, dict):
            return {k: self._substitute(v, ctx) for k, v in value.items()}
        if isinstance(value, list):
            return [self._substitute(v, ctx) for v in value]
        if isinstance(value, str):
            result = value

            # {{input}}
            result = result.replace("{{input}}", ctx.get("input", ""))

            # {{steps.N.result}}
            step_results = ctx.get("step_results", [])
            for m in re.finditer(r"\{\{steps\.(\d+)\.result\}\}", result):
                idx = int(m.group(1))
                replacement = str(step_results[idx]) if idx < len(step_results) else ""
                result = result.replace(m.group(0), replacement)

            # {{vars.name}}
            variables = ctx.get("variables", {})
            for m in re.finditer(r"\{\{vars\.(\w+)\}\}", result):
                result = result.replace(m.group(0), str(variables.get(m.group(1), "")))

            # {{loop.item}} and {{loop.index}}
            if "loop_item" in ctx:
                result = result.replace("{{loop.item}}", str(ctx["loop_item"]))
            if "loop_index" in ctx:
                result = result.replace("{{loop.index}}", str(ctx["loop_index"]))

            # {{webhook.field}}
            webhook = ctx.get("webhook", {})
            for m in re.finditer(r"\{\{webhook\.(\w+)\}\}", result):
                result = result.replace(m.group(0), str(webhook.get(m.group(1), "")))

            return result
        return value

    # ------------------------------------------------------------------
    # Condition evaluator
    # ------------------------------------------------------------------
    def _evaluate_condition(self, condition: str, ctx: dict) -> bool:
        """Evaluate a simple condition string after variable substitution.

        Operators: contains, not_contains, ==, !=, is_empty, not_empty
        """
        if not condition or not condition.strip():
            return True

        resolved = self._substitute(condition, ctx)

        # is_empty / not_empty (unary — the entire resolved string is the operand)
        if resolved.strip().endswith("is_empty"):
            operand = resolved.strip()[:-len("is_empty")].strip()
            return operand == ""
        if resolved.strip().endswith("not_empty"):
            operand = resolved.strip()[:-len("not_empty")].strip()
            return operand != ""

        # Binary operators
        for op in ["not_contains", "contains", "!=", "=="]:
            if f" {op} " in resolved:
                parts = resolved.split(f" {op} ", 1)
                left = parts[0].strip().strip('"').strip("'")
                right = parts[1].strip().strip('"').strip("'")
                if op == "contains":
                    return right in left
                elif op == "not_contains":
                    return right not in left
                elif op == "==":
                    return left == right
                elif op == "!=":
                    return left != right

        # If we can't parse it, default to True (run the step)
        return True

    # ------------------------------------------------------------------
    # Step executors by type
    # ------------------------------------------------------------------
    async def _run_tool_step(self, step: dict, ctx: dict, pseudo_conv: str) -> str:
        """Execute a standard tool step with retry support."""
        tool_name = step["tool"]
        raw_args = step.get("args", {})
        resolved_args = self._substitute(raw_args, ctx)

        max_retries = min(step.get("retry", 0), 3)
        retry_delay = step.get("retry_delay", 1.0)

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                result = await exec_tool(
                    self.http, self.events, tool_name, resolved_args, pseudo_conv
                )
                return result
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    await self.events.emit(pseudo_conv, "workflow_step_retry", {
                        "step": ctx.get("step_index", 0),
                        "name": step.get("name", ""),
                        "attempt": attempt + 1,
                        "max_retries": max_retries,
                        "error": str(e),
                    })
                    await asyncio.sleep(retry_delay * (2 ** attempt))

        raise last_error

    async def _run_ai_completion(self, step: dict, ctx: dict, pseudo_conv: str) -> str:
        """Single Ollama /api/generate call — AI as a tool, not orchestrator."""
        prompt = self._substitute(step.get("prompt", ""), ctx)
        model = step.get("model") or config.DEFAULT_MODEL
        system = self._substitute(step.get("system", ""), ctx) if step.get("system") else None

        await self.events.emit(pseudo_conv, "workflow_step", {
            "step": ctx.get("step_index", 0),
            "name": step.get("name", "AI Completion"),
            "status": "running",
            "detail": f"Model: {model}",
        })

        payload = {"model": model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system

        r = await self.http.post(f"{config.OLLAMA_URL}/api/generate", json=payload, timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"Ollama returned {r.status_code}: {r.text[:200]}")

        data = r.json()
        return data.get("response", "")

    async def _run_parallel(self, step: dict, ctx: dict, pseudo_conv: str) -> str:
        """Run sub-steps concurrently via asyncio.gather."""
        sub_steps = step.get("steps", [])
        if isinstance(sub_steps, str):
            sub_steps = json.loads(sub_steps)

        async def _exec_sub(i, s):
            sub_ctx = {**ctx, "step_index": f"{ctx.get('step_index', 0)}.{i}"}
            return await self._execute_single_step(s, sub_ctx, pseudo_conv)

        results = await asyncio.gather(
            *[_exec_sub(i, s) for i, s in enumerate(sub_steps)],
            return_exceptions=True
        )

        # Collect results, marking exceptions
        output_parts = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                output_parts.append(f"[Step {i} ERROR: {r}]")
            else:
                output_parts.append(str(r) if r else "")

        return "\n---\n".join(output_parts)

    async def _run_loop(self, step: dict, ctx: dict, pseudo_conv: str) -> str:
        """Iterate over a collection, running sub-steps for each item."""
        over_raw = self._substitute(step.get("over", ""), ctx)
        max_iter = step.get("max_iterations", 10)
        sub_steps = step.get("steps", [])
        if isinstance(sub_steps, str):
            sub_steps = json.loads(sub_steps)

        # Parse the collection
        try:
            items = json.loads(over_raw)
            if not isinstance(items, list):
                items = [items]
        except (json.JSONDecodeError, TypeError):
            items = [line for line in over_raw.splitlines() if line.strip()]

        items = items[:max_iter]
        all_results = []

        for idx, item in enumerate(items):
            await self.events.emit(pseudo_conv, "workflow_loop", {
                "step": ctx.get("step_index", 0),
                "iteration": idx,
                "total": len(items),
                "item_preview": str(item)[:80],
            })

            loop_ctx = {**ctx, "loop_item": str(item), "loop_index": str(idx)}
            iteration_results = []
            for s in sub_steps:
                result = await self._execute_single_step(s, loop_ctx, pseudo_conv)
                iteration_results.append(result)
            all_results.append(iteration_results[-1] if iteration_results else "")

        return "\n---\n".join(str(r) for r in all_results)

    async def _run_sub_workflow(self, step: dict, ctx: dict, pseudo_conv: str) -> str:
        """Execute another workflow as a sub-step."""
        wf_id = self._substitute(step.get("workflow_id", ""), ctx)
        input_text = self._substitute(step.get("input", ""), ctx)

        wf = await db.get_workflow(wf_id)
        if not wf:
            raise RuntimeError(f"Sub-workflow not found: {wf_id}")

        import uuid as _uuid
        sub_run_id = f"wfr-sub-{_uuid.uuid4().hex[:8]}"
        await db.create_workflow_run(sub_run_id, wf_id, pseudo_conv, input_text)

        sub_executor = WorkflowExecutor(self.http, self.events)
        results = await sub_executor.run(sub_run_id, wf, input_text, pseudo_conv)

        # Return the last step's result
        if results:
            last = results[-1]
            return last.get("result", last.get("error", ""))
        return ""

    # ------------------------------------------------------------------
    # Single step dispatcher
    # ------------------------------------------------------------------
    async def _execute_single_step(self, step: dict, ctx: dict, pseudo_conv: str) -> str:
        """Route a step to the appropriate executor based on its type."""
        step_type = step.get("type", "tool")

        if step_type == "ai_completion":
            return await self._run_ai_completion(step, ctx, pseudo_conv)
        elif step_type == "parallel":
            return await self._run_parallel(step, ctx, pseudo_conv)
        elif step_type == "loop":
            return await self._run_loop(step, ctx, pseudo_conv)
        elif step_type == "run_workflow":
            return await self._run_sub_workflow(step, ctx, pseudo_conv)
        else:
            # Default: tool step
            return await self._run_tool_step(step, ctx, pseudo_conv)

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------
    async def run(self, run_id: str, workflow: dict, input_text: str,
                  conv_id: str = None, webhook_data: dict = None):
        pseudo_conv = conv_id or f"wf-run-{run_id}"
        steps = workflow.get("steps", [])
        if isinstance(steps, str):
            steps = json.loads(steps)

        # Execution context — shared across all steps
        ctx = {
            "input": input_text,
            "step_results": [],
            "variables": {},
            "webhook": webhook_data or {},
        }
        all_step_data = []

        await db.update_workflow_run(run_id, status="running", started_at=time.time())
        await self.events.emit(pseudo_conv, "workflow_start", {
            "run_id": run_id,
            "workflow": workflow.get("name", ""),
            "total_steps": len(steps),
        })

        try:
          return await self._run_steps(run_id, steps, ctx, pseudo_conv, workflow, all_step_data)
        except Exception as e:
            # Catch-all: never leave a run stuck in "running"
            await db.update_workflow_run(
                run_id, status="failed", error=f"Unexpected error: {e}",
                step_results=json.dumps(all_step_data),
                completed_at=time.time(),
            )
            await self.events.emit(pseudo_conv, "workflow_error", {
                "run_id": run_id, "error": str(e),
            })
            return all_step_data

    async def _run_steps(self, run_id, steps, ctx, pseudo_conv, workflow, all_step_data):
        for i, step in enumerate(steps):
            step_name = step.get("name", f"Step {i}")
            ctx["step_index"] = i

            # --- Condition check ---
            condition = step.get("condition")
            if condition and not self._evaluate_condition(condition, ctx):
                step_record = {
                    "step_index": i, "name": step_name,
                    "type": step.get("type", "tool"),
                    "status": "skipped", "reason": f"Condition false: {condition}",
                    "started_at": time.time(), "completed_at": time.time(),
                }
                all_step_data.append(step_record)
                ctx["step_results"].append(None)
                await self.events.emit(pseudo_conv, "workflow_step", {
                    "step": i, "name": step_name,
                    "status": "skipped", "total": len(steps),
                })
                continue

            await self.events.emit(pseudo_conv, "workflow_step", {
                "step": i, "name": step_name,
                "status": "running", "total": len(steps),
                "type": step.get("type", "tool"),
            })

            step_record = {
                "step_index": i, "name": step_name,
                "type": step.get("type", "tool"),
                "tool": step.get("tool", ""),
                "started_at": time.time(), "status": "running",
            }

            try:
                result = await self._execute_single_step(step, ctx, pseudo_conv)
                step_record["result"] = result
                step_record["status"] = "completed"
                step_record["completed_at"] = time.time()
                ctx["step_results"].append(result)

                # Store named variable if specified
                output_var = step.get("output_var")
                if output_var:
                    ctx["variables"][output_var] = result

                await self.events.emit(pseudo_conv, "workflow_step", {
                    "step": i, "name": step_name,
                    "status": "completed", "total": len(steps),
                    "preview": (result[:200] if result else ""),
                    "duration": round(step_record["completed_at"] - step_record["started_at"], 2),
                })

            except Exception as e:
                step_record["status"] = "failed"
                step_record["error"] = str(e)
                step_record["completed_at"] = time.time()

                on_error = step.get("on_error", "fail")

                if on_error == "skip":
                    ctx["step_results"].append(None)
                    all_step_data.append(step_record)
                    await self.events.emit(pseudo_conv, "workflow_step", {
                        "step": i, "name": step_name,
                        "status": "skipped", "total": len(steps),
                        "error": str(e),
                    })
                    await db.update_workflow_run(run_id, step_results=json.dumps(all_step_data))
                    continue

                elif on_error == "continue":
                    ctx["step_results"].append(f"ERROR: {e}")
                    if step.get("output_var"):
                        ctx["variables"][step["output_var"]] = f"ERROR: {e}"
                    all_step_data.append(step_record)
                    await self.events.emit(pseudo_conv, "workflow_step", {
                        "step": i, "name": step_name,
                        "status": "failed", "total": len(steps),
                        "error": str(e), "continued": True,
                    })
                    await db.update_workflow_run(run_id, step_results=json.dumps(all_step_data))
                    continue

                else:  # "fail" — stop execution
                    all_step_data.append(step_record)
                    await db.update_workflow_run(
                        run_id, status="failed", error=str(e),
                        step_results=json.dumps(all_step_data),
                        completed_at=time.time(),
                    )
                    await self.events.emit(pseudo_conv, "workflow_error", {
                        "step": i, "name": step_name, "error": str(e),
                    })
                    return all_step_data

            all_step_data.append(step_record)
            await db.update_workflow_run(run_id, step_results=json.dumps(all_step_data))

        await db.update_workflow_run(
            run_id, status="completed",
            step_results=json.dumps(all_step_data),
            completed_at=time.time(),
        )
        await self.events.emit(pseudo_conv, "workflow_complete", {
            "run_id": run_id,
            "workflow": workflow.get("name", ""),
            "steps_completed": len(steps),
        })
        return all_step_data
