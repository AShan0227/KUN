"""Integration adapters — wire LT.A-G long-task services to KUN main path.

Each adapter wraps an existing KUN subsystem (LLMRouter / SkillDispatcher /
session_scope ORM writers / ExternalSupervisorService) into the callback
signature expected by long-task services (LT.B-E).

Layout (each file independent, can evolve separately):
  - llm_invoker.py        · LLMRouter → ExecutorLoop.LLMInvoker
  - tool_executor.py      · kun.skills.dispatcher → ExecutorLoop.ToolExecutor
  - checkpoint_db.py      · session_scope + TaskCheckpointRow ↔ TaskCheckpointService callbacks
  - plan_review_db.py     · session_scope + PlanReviewRow ← PlanReviewOutcome writer
  - external_supervisor.py · ExternalSupervisorService → PlanReviewService.ExternalSupervisorVerify

Composition: kun.engineering.long_task_orchestrator.LongTaskOrchestrator
assembles all five adapters + LT.B/C/D/E/F services into a single class with
`run_long_task(task_ref, on_event)` entry, callable from
`kun.engineering.orchestrator.Orchestrator.stream` when long-task mode triggers.
"""
